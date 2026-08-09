"""阿里云 OSS 相关服务：预签名 URL 直传、缩略图访问 URL、对象核验。

设计要点
--------
- **PUT 直传时把 Content-Type 一起签**，防止客户端上传成任意类型
  （若签名时没绑 Content-Type，攻击者可以传一个 .html 冒充图片）。
- **回调阶段用 head_object 核实对象真的存在且大小/类型匹配**，
  这样后端就能相信"客户端说传完了"这句话。
- 未配置 OSS 时走 **本地磁盘 mock**，把开发闭环打通而不需要真实 Bucket。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import oss2

from app.config import settings
from app.services.circuit_breaker import oss_breaker

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------
# 是否处于"未配置真实 OSS"的开发环境
# ------------------------------------------------------------------------
def _is_mock_mode() -> bool:
    return (
        not settings.oss_bucket
        or settings.oss_bucket == "photo-agent-dev"
        or settings.oss_key_id in ("", "LTAI_xxx")
    )


# 本地 mock 存放目录（放在 /tmp，随容器重启清空亦无妨）
_MOCK_ROOT = Path("/tmp/photo-agent-oss-mock")


# ------------------------------------------------------------------------
# 客户端与 key 构造
# ------------------------------------------------------------------------
def _bucket() -> oss2.Bucket:
    """按需构造 Bucket 客户端；调用前先确认非 mock。"""
    auth = oss2.Auth(settings.oss_key_id, settings.oss_key_secret)
    return oss2.Bucket(
        auth,
        f"https://{settings.oss_endpoint}",
        settings.oss_bucket,
        connect_timeout=10,
    )


# 允许的 MIME 前缀（防止有人签一个上传 exe 的 URL）
_ALLOWED_MIME_PREFIXES = ("image/",)


def _pick_ext(mime_type: str) -> str:
    """从 MIME 猜文件后缀，只允许图片类型。"""
    m = (mime_type or "").lower()
    if m in ("image/jpeg", "image/jpg"):
        return "jpg"
    if m == "image/png":
        return "png"
    if m == "image/webp":
        return "webp"
    if m in ("image/heic", "image/heif"):
        return "heic"
    if m == "image/gif":
        return "gif"
    # 其他一律拒绝
    return ""


def build_oss_key(user_id: str, hash_: str, mime_type: str) -> str:
    """OSS 对象 key 规则：photos/{user_id}/{yyyy/mm/dd}/{hash}.{ext}."""
    ext = _pick_ext(mime_type)
    if not ext:
        raise ValueError(f"Unsupported mime_type: {mime_type!r}")
    today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    return f"photos/{user_id}/{today}/{hash_}.{ext}"


# ------------------------------------------------------------------------
# 签名 URL
# ------------------------------------------------------------------------
@dataclass(slots=True)
class SignedPut:
    url: str
    headers: dict[str, str]   # 客户端 PUT 时必须回带这些头
    expires_in: int


def sign_put_url(
    oss_key: str,
    mime_type: str,
    ttl: int | None = None,
) -> SignedPut:
    """
    生成 PUT 直传预签名 URL，并把必须回带的 Header 一起告诉客户端。

    客户端调用示例（伪代码）::

        PUT <signed_url>
        Content-Type: <mime_type>
        Body: <binary>
    """
    if not any(mime_type.startswith(p) for p in _ALLOWED_MIME_PREFIXES):
        raise ValueError(f"Content-Type not allowed: {mime_type}")

    ttl = ttl or settings.oss_upload_ttl
    headers = {"Content-Type": mime_type}

    if _is_mock_mode():
        # 只返回 path，客户端拼上自己看到的 API base url 即可
        # 这样宿主机 curl 和 docker 内部都能用
        return SignedPut(
            url=f"/_mock/oss/{oss_key}",
            headers=headers,
            expires_in=ttl,
        )

    # 真实签名：oss2 支持在签名时绑定 headers，客户端上传时必须回带一致的头
    url = _bucket().sign_url(
        "PUT",
        oss_key,
        ttl,
        slash_safe=True,
        headers=headers,
    )
    return SignedPut(url=url, headers=headers, expires_in=ttl)


def sign_get_url(oss_key: str, ttl: int = 3600) -> str:
    """生成 GET 预签名 URL，用于让客户端下载/预览私有 Bucket 中的对象."""
    if not oss_key:
        return ""
    if _is_mock_mode():
        return f"/_mock/oss/{oss_key}"
    return _bucket().sign_url("GET", oss_key, ttl, slash_safe=True)


# ------------------------------------------------------------------------
# 核验：对象是否真的落到了 OSS 上
# ------------------------------------------------------------------------
@dataclass(slots=True)
class ObjectMeta:
    size: int
    content_type: str
    etag: str


def _head_object_sync(oss_key: str) -> ObjectMeta | None:
    """检查对象是否存在（同步内部实现）。返回 None 表示不存在。"""
    if _is_mock_mode():
        path = _MOCK_ROOT / oss_key
        if not path.is_file():
            return None
        return ObjectMeta(
            size=path.stat().st_size,
            content_type="image/jpeg",  # mock 阶段不精确
            etag=_mock_etag(path),
        )
    try:
        meta = _bucket().head_object(oss_key)
    except oss2.exceptions.NoSuchKey:
        return None
    except oss2.exceptions.NotFound:
        return None
    return ObjectMeta(
        size=int(meta.content_length),
        content_type=meta.content_type or "",
        etag=(meta.etag or "").strip('"'),
    )


async def head_object(oss_key: str) -> ObjectMeta | None:
    """检查对象是否存在（异步，真实模式经熔断器 + to_thread 保护）。"""
    if _is_mock_mode():
        return _head_object_sync(oss_key)
    return await oss_breaker.call(asyncio.to_thread, _head_object_sync, oss_key)


def _mock_etag(path: Path) -> str:
    """本地 mock 用文件 MD5 前 16 位当 etag."""
    md5 = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            md5.update(chunk)
    return md5.hexdigest()[:16]


# ------------------------------------------------------------------------
# 本地 mock 上传（仅当 _is_mock_mode() 为真时启用）
# ------------------------------------------------------------------------
def mock_write_object(oss_key: str, src_stream) -> ObjectMeta:
    """把上传的字节流写入本地 mock 磁盘。"""
    dst = _MOCK_ROOT / oss_key
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("wb") as f:
        shutil.copyfileobj(src_stream, f)
    return ObjectMeta(
        size=dst.stat().st_size,
        content_type="image/jpeg",
        etag=_mock_etag(dst),
    )


def mock_read_object(oss_key: str) -> bytes | None:
    path = _MOCK_ROOT / oss_key
    if not path.is_file():
        return None
    return path.read_bytes()


# ------------------------------------------------------------------------
# 通用：读对象 / 写对象（mock 与真实两条路径统一）
# ------------------------------------------------------------------------
def _get_object_sync(oss_key: str) -> bytes | None:
    """下载对象内容（同步内部实现）。worker 用这个把原图拉到内存里跑 AI。"""
    if _is_mock_mode():
        return mock_read_object(oss_key)
    try:
        result = _bucket().get_object(oss_key)
        return result.read()
    except oss2.exceptions.NoSuchKey:
        return None
    except oss2.exceptions.NotFound:
        return None


async def get_object(oss_key: str) -> bytes | None:
    """下载对象内容（异步，真实模式经熔断器 + to_thread 保护）。"""
    if _is_mock_mode():
        return _get_object_sync(oss_key)
    return await oss_breaker.call(asyncio.to_thread, _get_object_sync, oss_key)


def _put_object_sync(oss_key: str, data: bytes, content_type: str = "image/jpeg") -> None:
    """写对象（同步内部实现）。worker 用这个上传生成的缩略图。"""
    if _is_mock_mode():
        import io as _io
        mock_write_object(oss_key, _io.BytesIO(data))
        return
    _bucket().put_object(
        oss_key,
        data,
        headers={"Content-Type": content_type},
    )


async def put_object(oss_key: str, data: bytes, content_type: str = "image/jpeg") -> None:
    """写对象（异步，真实模式经熔断器 + to_thread 保护）。"""
    if _is_mock_mode():
        _put_object_sync(oss_key, data, content_type)
        return
    await oss_breaker.call(asyncio.to_thread, _put_object_sync, oss_key, data, content_type)


def _delete_object_sync(oss_key: str) -> None:
    """删除对象（同步内部实现）。"""
    if _is_mock_mode():
        path = _MOCK_ROOT / oss_key
        if path.is_file():
            path.unlink()
        return
    _bucket().delete_object(oss_key)


async def delete_object(oss_key: str) -> None:
    """删除对象（异步，真实模式经熔断器 + to_thread 保护）。"""
    if _is_mock_mode():
        _delete_object_sync(oss_key)
        return
    await oss_breaker.call(asyncio.to_thread, _delete_object_sync, oss_key)


def thumb_key_of(oss_key: str) -> str:
    """约定：photos/xxx/abc.jpg -> photos/xxx/abc.thumb.jpg"""
    if "." in oss_key.rsplit("/", 1)[-1]:
        base, ext = oss_key.rsplit(".", 1)
        return f"{base}.thumb.{ext}"
    return oss_key + ".thumb"


# ------------------------------------------------------------------------
# 杂项
# ------------------------------------------------------------------------
def new_upload_id() -> str:
    return uuid.uuid4().hex


def is_mock() -> bool:
    """外部模块（如 mock 端点）判断当前是否 mock 模式."""
    return _is_mock_mode()
