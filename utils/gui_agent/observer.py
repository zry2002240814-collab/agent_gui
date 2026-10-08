"""Only observes the image supplied by the framework, never queries the device."""

from dataclasses import dataclass


@dataclass
class Observation:
    step: int
    identifier: str
    fingerprint: int
    width: int
    height: int


def observe(image, step):
    small = image.convert("L").resize((32, 32))
    pixels = list(small.tobytes())
    mean = sum(pixels) / len(pixels)
    fingerprint = sum(1 << index for index, value in enumerate(pixels) if value >= mean)
    return Observation(step, f"obs-{step}", fingerprint, image.width, image.height)


def similar(first, second):
    return first is not None and (first.fingerprint ^ second.fingerprint).bit_count() <= 10
