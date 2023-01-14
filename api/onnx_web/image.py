from numpy import random
from PIL import Image
from typing import Tuple

import numpy as np

def blend_mask_inverse_source(source: Tuple[int, int, int], mask: Tuple[int, int, int], noise: int) -> Tuple[int, int, int]:
    m = float(noise) / 256
    n = 1.0 - m

    return (
        int((source[0] * n) + (mask[0] * m)),
        int((source[1] * n) + (mask[1] * m)),
        int((source[2] * n) + (mask[2] * m)),
    )


def blend_source_histogram(source_image: Image, dims: Tuple[int, int]) -> Tuple[float, float, float]:
    r, g, b = source_image.split()
    width, height = dims
    size = width * height

    hist_r = r.histogram()
    hist_g = g.histogram()
    hist_b = b.histogram()

    noise_r = random.choice(256, p=np.divide(np.copy(hist_r), np.sum(hist_r)), size=size)
    noise_g = random.choice(256, p=np.divide(np.copy(hist_g), np.sum(hist_g)), size=size)
    noise_b = random.choice(256, p=np.divide(np.copy(hist_b), np.sum(hist_b)), size=size)

    noise = Image.fromarray(np.reshape(np.dstack(noise_r, noise_g, noise_b), size*3))

    return noise



# based on https://github.com/AUTOMATIC1111/stable-diffusion-webui/blob/master/scripts/outpainting_mk_2.py#L175-L232
def expand_image(source_image: Image, mask_image: Image, dims: Tuple[int, int, int, int], fill = 'white', blend_source=blend_source_histogram, blend_op=blend_mask_inverse_source):
    left, right, top, bottom = dims

    full_width = left + source_image.width + right
    full_height = top + source_image.height + bottom

    full_source = Image.new('RGB', (full_width, full_height), fill)
    full_source.paste(source_image, (left, top))

    full_mask = Image.new('RGB', (full_width, full_height), fill)
    full_mask.paste(mask_image, (left, top))

    full_noise = blend_source(source_image, (full_width, full_height))

    for x in range(full_source.width):
        for y in range(full_source.height):
            mask_color = full_mask.getpixel((x, y))
            noise_color = full_noise.getpixel((x, y))
            source_color = full_source.getpixel((x, y))

            if mask_color[0] > 0:
                full_source.putpixel((x, y), blend_op(source_color, mask_color, noise_color))

    return (full_source, full_mask, (full_width, full_height))