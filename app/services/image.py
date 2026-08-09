"""图片处理：抽 EXIF、生成缩略图。全部纯 CPU、纯本地 IO，不涉及外部服务。

worker 从 OSS 读原图字节流后调用这里；结果由 worker 决定回写哪些字段。
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PIL import ExifTags, Image

logger = logging.getLogger(__name__)

# EXIF 标签名 -> 编号 反向映射，用来查我们关心的字段
_EXIF_KEYS = {v: k for k, v in ExifTags.TAGS.items()}


@dataclass(slots=True)
class ProcessedImage:
    width: int
    height: int
    taken_at: datetime | None
    location: dict[str, Any] | None
    thumb_bytes: bytes           # JPEG 缩略图字节
    thumb_size: tuple[int, int]  # (w, h)


def process(image_bytes: bytes, thumb_max: int = 512) -> ProcessedImage:
    """从原图字节抽出 EXIF + 尺寸，生成不超过 thumb_max 边的缩略图。"""
    img = Image.open(io.BytesIO(image_bytes))
    img.load()

    width, height = img.size
    taken_at = _parse_taken_at(img)
    location = _parse_gps(img)

    # 缩略图：等比缩放，保 EXIF 方向
    try:
        # Pillow 9.1+ 用 Transpose，之前用整数常量
        img_thumb = _apply_exif_orientation(img)
    except Exception:  # noqa: BLE001
        img_thumb = img.copy()

    img_thumb.thumbnail((thumb_max, thumb_max))
    # JPEG 不支持 alpha，统一转 RGB
    if img_thumb.mode != "RGB":
        img_thumb = img_thumb.convert("RGB")

    buf = io.BytesIO()
    img_thumb.save(buf, format="JPEG", quality=82, optimize=True)

    return ProcessedImage(
        width=width,
        height=height,
        taken_at=taken_at,
        location=location,
        thumb_bytes=buf.getvalue(),
        thumb_size=img_thumb.size,
    )


# ---- EXIF 辅助 --------------------------------------------------------


def _get_exif(img: Image.Image) -> dict[int, Any]:
    """兼容 Pillow 各版本，返回 {tag_id: value}。没有 EXIF 就返回空。"""
    try:
        raw = img.getexif()
    except Exception:  # noqa: BLE001
        return {}
    return dict(raw) if raw else {}


def _parse_taken_at(img: Image.Image) -> datetime | None:
    exif = _get_exif(img)
    for name in ("DateTimeOriginal", "DateTime"):
        tag = _EXIF_KEYS.get(name)
        if tag and tag in exif:
            v = exif[tag]
            try:
                # EXIF 时间格式：2024:03:15 09:30:20
                return datetime.strptime(v, "%Y:%m:%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
            except (ValueError, TypeError):
                pass
    return None


def _parse_gps(img: Image.Image) -> dict[str, Any] | None:
    """把 EXIF 里的 GPS 段转成 {"lat": ..., "lng": ...}。没数据返回 None。"""
    exif = _get_exif(img)
    gps_tag = _EXIF_KEYS.get("GPSInfo")
    if not gps_tag or gps_tag not in exif:
        return None
    gps = exif[gps_tag]
    if not isinstance(gps, dict):
        return None

    def _dms_to_deg(dms) -> float:
        d, m, s = dms
        return float(d) + float(m) / 60 + float(s) / 3600

    try:
        lat = _dms_to_deg(gps[2])
        if gps.get(1) == "S":
            lat = -lat
        lng = _dms_to_deg(gps[4])
        if gps.get(3) == "W":
            lng = -lng
        return {"lat": round(lat, 6), "lng": round(lng, 6)}
    except (KeyError, TypeError, ValueError):
        return None


def _apply_exif_orientation(img: Image.Image) -> Image.Image:
    """根据 EXIF Orientation 旋转/翻转图片，避免竖着拍变成横的。"""
    exif = _get_exif(img)
    orient_tag = _EXIF_KEYS.get("Orientation")
    if not orient_tag or orient_tag not in exif:
        return img.copy()
    orientation = exif[orient_tag]
    ops = {
        2: Image.FLIP_LEFT_RIGHT,
        3: Image.ROTATE_180,
        4: Image.FLIP_TOP_BOTTOM,
        5: (Image.FLIP_LEFT_RIGHT, Image.ROTATE_270),
        6: Image.ROTATE_270,
        7: (Image.FLIP_LEFT_RIGHT, Image.ROTATE_90),
        8: Image.ROTATE_90,
    }
    op = ops.get(orientation)
    if op is None:
        return img.copy()
    if isinstance(op, tuple):
        out = img
        for step in op:
            out = out.transpose(step)
        return out
    return img.transpose(op)
