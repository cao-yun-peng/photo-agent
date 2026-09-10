"""Bounded thumbnail similarity hints, never original-photo deletion."""

import asyncio
import io
from PIL import Image, ImageOps
from app.services import oss


def fingerprint(content):
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("thumbnail too large")
    with Image.open(io.BytesIO(content)) as raw:
        if raw.width * raw.height > 4_000_000 or getattr(raw, "n_frames", 1) != 1:
            raise ValueError("unsupported thumbnail")
        image = ImageOps.exif_transpose(raw).convert("RGB")
        ratio = image.width / image.height
        average = tuple(image.resize((1, 1)).getpixel((0, 0)))
        pixels = list(image.convert("L").resize((9, 8)).getdata())
        bits = 0
        for y in range(8):
            for x in range(8):
                bits = (bits << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
        return bits, average, ratio


def similar(a, b):
    return (
        (a[0] ^ b[0]).bit_count() <= 4
        and max(abs(x - y) for x, y in zip(a[1], b[1])) <= 24
        and abs(a[2] - b[2]) / max(a[2], b[2]) <= 0.08
    )


async def thumbnail_groups(candidates):
    gate = asyncio.Semaphore(4)
    values = {}

    async def inspect(photo):
        if not photo.thumb_key:
            return
        async with gate:
            try:
                content = await oss.get_object(photo.thumb_key)
                values[str(photo.id)] = fingerprint(content)
            except Exception:
                return

    tasks = [asyncio.create_task(inspect(p)) for p in candidates]
    if tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=8)
        except TimeoutError:
            pass
    # Complete-link grouping avoids a chain of weak matches merging dissimilar ends.
    groups = []
    for photo in candidates:
        pid = str(photo.id)
        if pid not in values:
            continue
        value = values[pid]
        target = next(
            (g for g in groups if all(similar(value, values[other]) for other in g)),
            None,
        )
        if target is None:
            groups.append([pid])
        else:
            target.append(pid)
    duplicates = [g for g in groups if len(g) > 1]
    mapping = {pid: index for index, group in enumerate(duplicates) for pid in group}
    return mapping, {
        "checked": len(values),
        "total": len(candidates),
        "groups": duplicates,
        "method": "thumbnail dHash + average color + aspect ratio; heuristic",
    }
