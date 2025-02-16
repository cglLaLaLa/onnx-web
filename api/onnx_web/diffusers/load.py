from logging import getLogger
from os import path
from typing import Any, List, Optional, Tuple

from onnx import load_model
from transformers import CLIPTokenizer

from ..constants import ONNX_MODEL
from ..convert.diffusion.lora import blend_loras, buffer_external_data_tensors
from ..convert.diffusion.textual_inversion import blend_textual_inversions
from ..diffusers.pipelines.upscale import OnnxStableDiffusionUpscalePipeline
from ..diffusers.utils import expand_prompt
from ..params import DeviceParams, ImageParams
from ..server import ModelTypes, ServerContext
from ..utils import run_gc
from .patches.unet import UNetWrapper
from .patches.vae import VAEWrapper
from .pipelines.controlnet import OnnxStableDiffusionControlNetPipeline
from .pipelines.lpw import OnnxStableDiffusionLongPromptWeightingPipeline
from .pipelines.panorama import OnnxStableDiffusionPanoramaPipeline
from .pipelines.pix2pix import OnnxStableDiffusionInstructPix2PixPipeline
from .version_safe_diffusers import (
    DDIMScheduler,
    DDPMScheduler,
    DEISMultistepScheduler,
    DPMSolverMultistepScheduler,
    DPMSolverSinglestepScheduler,
    EulerAncestralDiscreteScheduler,
    EulerDiscreteScheduler,
    HeunDiscreteScheduler,
    IPNDMScheduler,
    KarrasVeScheduler,
    KDPM2AncestralDiscreteScheduler,
    KDPM2DiscreteScheduler,
    LMSDiscreteScheduler,
    OnnxRuntimeModel,
    OnnxStableDiffusionImg2ImgPipeline,
    OnnxStableDiffusionInpaintPipeline,
    OnnxStableDiffusionPipeline,
    PNDMScheduler,
    StableDiffusionPipeline,
    UniPCMultistepScheduler,
)

logger = getLogger(__name__)

available_pipelines = {
    "controlnet": OnnxStableDiffusionControlNetPipeline,
    "img2img": OnnxStableDiffusionImg2ImgPipeline,
    "inpaint": OnnxStableDiffusionInpaintPipeline,
    "lpw": OnnxStableDiffusionLongPromptWeightingPipeline,
    "panorama": OnnxStableDiffusionPanoramaPipeline,
    "pix2pix": OnnxStableDiffusionInstructPix2PixPipeline,
    "txt2img": OnnxStableDiffusionPipeline,
    "upscale": OnnxStableDiffusionUpscalePipeline,
}

pipeline_schedulers = {
    "ddim": DDIMScheduler,
    "ddpm": DDPMScheduler,
    "deis-multi": DEISMultistepScheduler,
    "dpm-multi": DPMSolverMultistepScheduler,
    "dpm-single": DPMSolverSinglestepScheduler,
    "euler": EulerDiscreteScheduler,
    "euler-a": EulerAncestralDiscreteScheduler,
    "heun": HeunDiscreteScheduler,
    "ipndm": IPNDMScheduler,
    "k-dpm-2-a": KDPM2AncestralDiscreteScheduler,
    "k-dpm-2": KDPM2DiscreteScheduler,
    "karras-ve": KarrasVeScheduler,
    "lms-discrete": LMSDiscreteScheduler,
    "pndm": PNDMScheduler,
    "unipc-multi": UniPCMultistepScheduler,
}


def get_available_pipelines() -> List[str]:
    return list(available_pipelines.keys())


def get_pipeline_schedulers() -> List[str]:
    return list(pipeline_schedulers.keys())


def get_scheduler_name(scheduler: Any) -> Optional[str]:
    for k, v in pipeline_schedulers.items():
        if scheduler == v or scheduler == v.__name__:
            return k

    return None


def load_pipeline(
    server: ServerContext,
    params: ImageParams,
    pipeline: str,
    device: DeviceParams,
    inversions: Optional[List[Tuple[str, float]]] = None,
    loras: Optional[List[Tuple[str, float]]] = None,
    model: Optional[str] = None,
):
    inversions = inversions or []
    loras = loras or []
    model = model or params.model

    torch_dtype = server.torch_dtype()
    logger.debug("using Torch dtype %s for pipeline", torch_dtype)

    control_key = params.control.name if params.control is not None else None
    pipe_key = (
        pipeline,
        model,
        device.device,
        device.provider,
        control_key,
        inversions,
        loras,
    )
    scheduler_key = (params.scheduler, model)
    scheduler_type = pipeline_schedulers[params.scheduler]

    cache_pipe = server.cache.get(ModelTypes.diffusion, pipe_key)

    if cache_pipe is not None:
        logger.debug("reusing existing diffusion pipeline")
        pipe = cache_pipe

        # update scheduler
        cache_scheduler = server.cache.get(ModelTypes.scheduler, scheduler_key)
        if cache_scheduler is None:
            logger.debug("loading new diffusion scheduler")
            scheduler = scheduler_type.from_pretrained(
                model,
                provider=device.ort_provider(),
                sess_options=device.sess_options(),
                subfolder="scheduler",
                torch_dtype=torch_dtype,
            )

            if device is not None and hasattr(scheduler, "to"):
                scheduler = scheduler.to(device.torch_str())

            pipe.scheduler = scheduler
            server.cache.set(ModelTypes.scheduler, scheduler_key, scheduler)
            run_gc([device])

    else:
        if server.cache.drop("diffusion", pipe_key) > 0:
            logger.debug("unloading previous diffusion pipeline")
            run_gc([device])

        logger.debug("loading new diffusion pipeline from %s", model)
        components = {
            "scheduler": scheduler_type.from_pretrained(
                model,
                provider=device.ort_provider(),
                sess_options=device.sess_options(),
                subfolder="scheduler",
                torch_dtype=torch_dtype,
            )
        }

        # shared components
        text_encoder = None
        unet_type = "unet"

        # ControlNet component
        if pipeline == "controlnet" and params.control is not None:
            cnet_path = path.join(
                server.model_path, "control", f"{params.control.name}.onnx"
            )
            logger.debug("loading ControlNet weights from %s", cnet_path)
            components["controlnet"] = OnnxRuntimeModel(
                OnnxRuntimeModel.load_model(
                    cnet_path,
                    provider=device.ort_provider(),
                    sess_options=device.sess_options(),
                )
            )

            unet_type = "cnet"

        # Textual Inversion blending
        if inversions is not None and len(inversions) > 0:
            logger.debug("blending Textual Inversions from %s", inversions)
            inversion_names, inversion_weights = zip(*inversions)

            inversion_models = [
                path.join(server.model_path, "inversion", name)
                for name in inversion_names
            ]
            text_encoder = load_model(path.join(model, "text_encoder", ONNX_MODEL))
            tokenizer = CLIPTokenizer.from_pretrained(
                model,
                subfolder="tokenizer",
                torch_dtype=torch_dtype,
            )
            text_encoder, tokenizer = blend_textual_inversions(
                server,
                text_encoder,
                tokenizer,
                list(
                    zip(
                        inversion_models,
                        inversion_weights,
                        inversion_names,
                        [None] * len(inversion_models),
                    )
                ),
            )

            components["tokenizer"] = tokenizer

            # should be pretty small and should not need external data
            if loras is None or len(loras) == 0:
                components["text_encoder"] = OnnxRuntimeModel(
                    OnnxRuntimeModel.load_model(
                        text_encoder.SerializeToString(),
                        provider=device.ort_provider("text-encoder"),
                        sess_options=device.sess_options(),
                    )
                )

        # LoRA blending
        if loras is not None and len(loras) > 0:
            lora_names, lora_weights = zip(*loras)
            lora_models = [
                path.join(server.model_path, "lora", name) for name in lora_names
            ]
            logger.info(
                "blending base model %s with LoRA models: %s", model, lora_models
            )

            # blend and load text encoder
            text_encoder = text_encoder or path.join(model, "text_encoder", ONNX_MODEL)
            text_encoder = blend_loras(
                server,
                text_encoder,
                list(zip(lora_models, lora_weights)),
                "text_encoder",
            )
            (text_encoder, text_encoder_data) = buffer_external_data_tensors(
                text_encoder
            )
            text_encoder_names, text_encoder_values = zip(*text_encoder_data)
            text_encoder_opts = device.sess_options(cache=False)
            text_encoder_opts.add_external_initializers(
                list(text_encoder_names), list(text_encoder_values)
            )
            components["text_encoder"] = OnnxRuntimeModel(
                OnnxRuntimeModel.load_model(
                    text_encoder.SerializeToString(),
                    provider=device.ort_provider("text-encoder"),
                    sess_options=text_encoder_opts,
                )
            )

            # blend and load unet
            unet = path.join(model, unet_type, ONNX_MODEL)
            blended_unet = blend_loras(
                server,
                unet,
                list(zip(lora_models, lora_weights)),
                "unet",
            )
            (unet_model, unet_data) = buffer_external_data_tensors(blended_unet)
            unet_names, unet_values = zip(*unet_data)
            unet_opts = device.sess_options(cache=False)
            unet_opts.add_external_initializers(list(unet_names), list(unet_values))
            components["unet"] = OnnxRuntimeModel(
                OnnxRuntimeModel.load_model(
                    unet_model.SerializeToString(),
                    provider=device.ort_provider("unet"),
                    sess_options=unet_opts,
                )
            )

        # make sure a UNet has been loaded
        if "unet" not in components:
            unet = path.join(model, unet_type, ONNX_MODEL)
            logger.debug("loading UNet (%s) from %s", unet_type, unet)
            components["unet"] = OnnxRuntimeModel(
                OnnxRuntimeModel.load_model(
                    unet,
                    provider=device.ort_provider("unet"),
                    sess_options=device.sess_options(),
                )
            )

        # one or more VAE models need to be loaded
        vae = path.join(model, "vae", ONNX_MODEL)
        vae_decoder = path.join(model, "vae_decoder", ONNX_MODEL)
        vae_encoder = path.join(model, "vae_encoder", ONNX_MODEL)

        if path.exists(vae):
            logger.debug("loading VAE from %s", vae)
            components["vae"] = OnnxRuntimeModel(
                OnnxRuntimeModel.load_model(
                    vae,
                    provider=device.ort_provider("vae"),
                    sess_options=device.sess_options(),
                )
            )
        elif path.exists(vae_decoder) and path.exists(vae_encoder):
            logger.debug("loading VAE decoder from %s", vae_decoder)
            components["vae_decoder"] = OnnxRuntimeModel(
                OnnxRuntimeModel.load_model(
                    vae_decoder,
                    provider=device.ort_provider("vae"),
                    sess_options=device.sess_options(),
                )
            )

            logger.debug("loading VAE encoder from %s", vae_encoder)
            components["vae_encoder"] = OnnxRuntimeModel(
                OnnxRuntimeModel.load_model(
                    vae_encoder,
                    provider=device.ort_provider("vae"),
                    sess_options=device.sess_options(),
                )
            )

        # additional options for panorama pipeline
        if pipeline == "panorama":
            components["window"] = params.tiles // 8
            components["stride"] = params.stride // 8

        pipeline_class = available_pipelines.get(pipeline, OnnxStableDiffusionPipeline)
        logger.debug("loading pretrained SD pipeline for %s", pipeline_class.__name__)
        pipe = pipeline_class.from_pretrained(
            model,
            provider=device.ort_provider(),
            sess_options=device.sess_options(),
            safety_checker=None,
            torch_dtype=torch_dtype,
            **components,
        )

        if not server.show_progress:
            pipe.set_progress_bar_config(disable=True)

        optimize_pipeline(server, pipe)
        patch_pipeline(server, pipe, pipeline, pipeline_class, params)

        server.cache.set(ModelTypes.diffusion, pipe_key, pipe)
        server.cache.set(ModelTypes.scheduler, scheduler_key, components["scheduler"])

    if hasattr(pipe, "vae_decoder"):
        pipe.vae_decoder.set_tiled(tiled=params.tiled_vae)
    if hasattr(pipe, "vae_encoder"):
        pipe.vae_encoder.set_tiled(tiled=params.tiled_vae)

    # update panorama params
    if pipeline == "panorama":
        latent_window = params.tiles // 8
        latent_stride = params.stride // 8

        pipe.set_window_size(latent_window, latent_stride)
        if hasattr(pipe, "vae_decoder"):
            pipe.vae_decoder.set_window_size(latent_window, params.overlap)
        if hasattr(pipe, "vae_encoder"):
            pipe.vae_encoder.set_window_size(latent_window, params.overlap)

    return pipe


def optimize_pipeline(
    server: ServerContext,
    pipe: StableDiffusionPipeline,
) -> None:
    if (
        "diffusers-attention-slicing" in server.optimizations
        or "diffusers-attention-slicing-auto" in server.optimizations
    ):
        logger.debug("enabling auto attention slicing on SD pipeline")
        try:
            pipe.enable_attention_slicing(slice_size="auto")
        except Exception as e:
            logger.warning("error while enabling auto attention slicing: %s", e)

    if "diffusers-attention-slicing-max" in server.optimizations:
        logger.debug("enabling max attention slicing on SD pipeline")
        try:
            pipe.enable_attention_slicing(slice_size="max")
        except Exception as e:
            logger.warning("error while enabling max attention slicing: %s", e)

    if "diffusers-vae-slicing" in server.optimizations:
        logger.debug("enabling VAE slicing on SD pipeline")
        try:
            pipe.enable_vae_slicing()
        except Exception as e:
            logger.warning("error while enabling VAE slicing: %s", e)

    if "diffusers-cpu-offload-sequential" in server.optimizations:
        logger.debug("enabling sequential CPU offload on SD pipeline")
        try:
            pipe.enable_sequential_cpu_offload()
        except Exception as e:
            logger.warning("error while enabling sequential CPU offload: %s", e)

    elif "diffusers-cpu-offload-model" in server.optimizations:
        # TODO: check for accelerate
        logger.debug("enabling model CPU offload on SD pipeline")
        try:
            pipe.enable_model_cpu_offload()
        except Exception as e:
            logger.warning("error while enabling model CPU offload: %s", e)

    if "diffusers-memory-efficient-attention" in server.optimizations:
        # TODO: check for xformers
        logger.debug("enabling memory efficient attention for SD pipeline")
        try:
            pipe.enable_xformers_memory_efficient_attention()
        except Exception as e:
            logger.warning("error while enabling memory efficient attention: %s", e)


def patch_pipeline(
    server: ServerContext,
    pipe: StableDiffusionPipeline,
    pipe_type: str,
    pipeline: Any,
    params: ImageParams,
) -> None:
    logger.debug("patching SD pipeline")

    if pipe_type != "lpw":
        pipe._encode_prompt = expand_prompt.__get__(pipe, pipeline)

    original_unet = pipe.unet
    pipe.unet = UNetWrapper(server, original_unet)

    if hasattr(pipe, "vae_decoder"):
        original_decoder = pipe.vae_decoder
        pipe.vae_decoder = VAEWrapper(
            server,
            original_decoder,
            decoder=True,
            window=params.tiles,
            overlap=params.overlap,
        )
        original_encoder = pipe.vae_encoder
        pipe.vae_encoder = VAEWrapper(
            server,
            original_encoder,
            decoder=False,
            window=params.tiles,
            overlap=params.overlap,
        )
    elif hasattr(pipe, "vae"):
        pass  # TODO: current wrapper does not work with upscaling VAE
    else:
        logger.debug("no VAE found to patch")
