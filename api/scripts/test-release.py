import sys
import traceback
from collections import Counter
from io import BytesIO
from logging import getLogger
from logging.config import dictConfig
from os import environ, path
from time import sleep
from typing import Optional

import cv2
import numpy as np
import requests
from PIL import Image
from yaml import safe_load

logging_path = environ.get("ONNX_WEB_LOGGING_PATH", "./logging.yaml")

try:
    if path.exists(logging_path):
        with open(logging_path, "r") as f:
            config_logging = safe_load(f)
            dictConfig(config_logging)
except Exception as err:
    print("error loading logging config: %s" % (err))

logger = getLogger(__name__)


def test_root() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    else:
        return "http://127.0.0.1:5000"


def test_path(relpath: str) -> str:
    return path.join(path.dirname(__file__), relpath)


class TestCase:
    def __init__(
        self,
        name: str,
        query: str,
        max_attempts: int = 20,
        mse_threshold: float = 0.001,
        source: Image.Image = None,
        mask: Image.Image = None,
    ) -> None:
        self.name = name
        self.query = query
        self.max_attempts = max_attempts
        self.mse_threshold = mse_threshold
        self.source = source
        self.mask = mask


TEST_DATA = [
    TestCase(
        "txt2img-sd-v1-5-256-muffin",
        "txt2img?prompt=a+giant+muffin&seed=0&scheduler=ddim&width=256&height=256",
    ),
    TestCase(
        "txt2img-sd-v1-5-512-muffin",
        "txt2img?prompt=a+giant+muffin&seed=0&scheduler=ddim",
    ),
    TestCase(
        "txt2img-sd-v1-5-512-muffin-deis",
        "txt2img?prompt=a+giant+muffin&seed=0&scheduler=deis",
    ),
    TestCase(
        "txt2img-sd-v1-5-512-muffin-dpm",
        "txt2img?prompt=a+giant+muffin&seed=0&scheduler=dpm-multi",
    ),
    TestCase(
        "txt2img-sd-v1-5-512-muffin-heun",
        "txt2img?prompt=a+giant+muffin&seed=0&scheduler=heun",
    ),
    TestCase(
        "txt2img-sd-v2-1-512-muffin",
        "txt2img?prompt=a+giant+muffin&seed=0&scheduler=ddim&model=stable-diffusion-onnx-v2-1",
    ),
    TestCase(
        "txt2img-sd-v2-1-768-muffin",
        "txt2img?prompt=a+giant+muffin&seed=0&scheduler=ddim&model=stable-diffusion-onnx-v2-1&width=768&height=768",
    ),
    TestCase(
        "txt2img-openjourney-512-muffin",
        "txt2img?prompt=mdjrny-v4+style+a+giant+muffin&seed=0&scheduler=ddim&model=diffusion-openjourney",
    ),
    TestCase(
        "txt2img-knollingcase-512-muffin",
        "txt2img?prompt=knollingcase+display+case+with+a+giant+muffin&seed=0&scheduler=ddim&model=diffusion-knollingcase",
    ),
    TestCase(
        "img2img-sd-v1-5-512-pumpkin",
        "img2img?prompt=a+giant+pumpkin&seed=0&scheduler=ddim",
        source="txt2img-sd-v1-5-512-muffin",
    ),
    TestCase(
        "img2img-sd-v1-5-256-pumpkin",
        "img2img?prompt=a+giant+pumpkin&seed=0&scheduler=ddim",
        source="txt2img-sd-v1-5-256-muffin",
    ),
    TestCase(
        "inpaint-v1-512-white",
        "inpaint?prompt=a+giant+pumpkin&seed=0&scheduler=ddim&model=stable-diffusion-onnx-v1-inpainting",
        source="txt2img-sd-v1-5-512-muffin",
        mask="mask-white",
    ),
    TestCase(
        "inpaint-v1-512-black",
        "inpaint?prompt=a+giant+pumpkin&seed=0&scheduler=ddim&model=stable-diffusion-onnx-v1-inpainting",
        source="txt2img-sd-v1-5-512-muffin",
        mask="mask-black",
    ),
    TestCase(
        "outpaint-even-256",
        (
            "inpaint?prompt=a+giant+pumpkin&seed=0&scheduler=ddim&model=stable-diffusion-onnx-v1-inpainting&noise=fill-mask"
            "&top=256&bottom=256&left=256&right=256"
        ),
        source="txt2img-sd-v1-5-512-muffin",
        mask="mask-black",
        mse_threshold=0.025,
    ),
    TestCase(
        "outpaint-vertical-512",
        (
            "inpaint?prompt=a+giant+pumpkin&seed=0&scheduler=ddim&model=stable-diffusion-onnx-v1-inpainting&noise=fill-mask"
            "&top=512&bottom=512&left=0&right=0"
        ),
        source="txt2img-sd-v1-5-512-muffin",
        mask="mask-black",
        mse_threshold=0.025,
    ),
    TestCase(
        "outpaint-horizontal-512",
        (
            "inpaint?prompt=a+giant+pumpkin&seed=0&scheduler=ddim&model=stable-diffusion-onnx-v1-inpainting&noise=fill-mask"
            "&top=0&bottom=0&left=512&right=512"
        ),
        source="txt2img-sd-v1-5-512-muffin",
        mask="mask-black",
        mse_threshold=0.025,
    ),
    TestCase(
        "upscale-resrgan-x4-2048-muffin",
        "upscale?prompt=a+giant+pumpkin&seed=0&scheduler=ddim&upscaling=upscaling-real-esrgan-x4-plus&scale=4&outscale=4",
        source="txt2img-sd-v1-5-512-muffin",
    ),
    TestCase(
        "upscale-resrgan-x2-1024-muffin",
        "upscale?prompt=a+giant+pumpkin&seed=0&scheduler=ddim&upscaling=upscaling-real-esrgan-x2-plus&scale=2&outscale=2",
        source="txt2img-sd-v1-5-512-muffin",
    ),
]


def generate_image(root: str, test: TestCase) -> Optional[str]:
    files = {}
    if test.source is not None:
        logger.debug("loading test source: %s", test.source)
        source_path = test_path(path.join("test-refs", f"{test.source}.png"))
        source_image = Image.open(source_path)
        source_bytes = BytesIO()
        source_image.save(source_bytes, "png")
        source_bytes.seek(0)
        files["source"] = source_bytes

    if test.mask is not None:
        logger.debug("loading test mask: %s", test.mask)
        mask_path = test_path(path.join("test-refs", f"{test.mask}.png"))
        mask_image = Image.open(mask_path)
        mask_bytes = BytesIO()
        mask_image.save(mask_bytes, "png")
        mask_bytes.seek(0)
        files["mask"] = mask_bytes

    logger.debug("generating image: %s", test.query)
    resp = requests.post(f"{root}/api/{test.query}", files=files)
    if resp.status_code == 200:
        json = resp.json()
        return json.get("output")
    else:
        logger.warning("request failed: %s", resp.status_code)
        return None


def check_ready(root: str, key: str) -> bool:
    resp = requests.get(f"{root}/api/ready?output={key}")
    if resp.status_code == 200:
        json = resp.json()
        return json.get("ready", False)
    else:
        logger.warning("request failed: %s", resp.status_code)
        return False


def download_image(root: str, key: str) -> Image.Image:
    resp = requests.get(f"{root}/output/{key}")
    if resp.status_code == 200:
        logger.debug("downloading image: %s", key)
        return Image.open(BytesIO(resp.content))
    else:
        logger.warning("request failed: %s", resp.status_code)
        return None


def find_mse(result: Image.Image, ref: Image.Image) -> float:
    if result.mode != ref.mode:
        logger.warning("image mode does not match: %s vs %s", result.mode, ref.mode)
        return float("inf")

    if result.size != ref.size:
        logger.warning("image size does not match: %s vs %s", result.size, ref.size)
        return float("inf")

    nd_result = np.array(result)
    nd_ref = np.array(ref)

    # dividing before squaring reduces the error into the lower end of the [0, 1] range
    diff = cv2.subtract(nd_ref, nd_result) / 255.0
    diff = np.sum(diff**2)

    return diff / (float(ref.height * ref.width))


def run_test(
    root: str,
    test: TestCase,
    ref: Image.Image,
) -> bool:
    """
    Generate an image, wait for it to be ready, and calculate the MSE from the reference.
    """

    key = generate_image(root, test)
    if key is None:
        raise ValueError("could not generate")

    attempts = 0
    while attempts < test.max_attempts:
        if check_ready(root, key):
            logger.debug("image is ready: %s", key)
            break
        else:
            logger.debug("waiting for image to be ready")
            attempts += 1
            sleep(6)

    if attempts == test.max_attempts:
        raise ValueError("image was not ready in time")

    result = download_image(root, key)
    result.save(test_path(path.join("test-results", f"{test.name}.png")))
    mse = find_mse(result, ref)

    if mse < test.mse_threshold:
        logger.info("MSE within threshold: %.4f < %.4f", mse, test.mse_threshold)
        return True
    else:
        logger.warning("MSE above threshold: %.4f > %.4f", mse, test.mse_threshold)
        return False


def main():
    root = test_root()
    logger.info("running release tests against API: %s", root)

    passed = []
    failed = []
    for test in TEST_DATA:
        try:
            logger.info("starting test: %s", test.name)
            ref_name = test_path(path.join("test-refs", f"{test.name}.png"))
            ref = Image.open(ref_name) if path.exists(ref_name) else None
            if run_test(root, test, ref):
                logger.info("test passed: %s", test.name)
                passed.append(test.name)
            else:
                logger.warning("test failed: %s", test.name)
                failed.append(test.name)
        except Exception as e:
            traceback.print_exception(type(e), e, e.__traceback__)
            logger.error("error running test for %s: %s", test.name, e)
            failed.append(test.name)

    logger.info("%s of %s tests passed", len(passed), len(TEST_DATA))
    if len(failed) > 0:
        logger.error("%s tests had errors", len(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()