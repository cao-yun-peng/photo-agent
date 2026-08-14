"""将人工复核图片集安全导入隔离的 Photo Agent 测试用户。

原图进入 OSS，数据库仅保存正常业务元数据；可把 pending 照片送入现有 ARQ
Worker 生成缩略图、视觉分析和向量。脚本按 (user_id, sha256) 幂等，可中断续跑。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import select, text

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.models.photo import Photo  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.oss import head_object, put_object  # noqa: E402

DEFAULT_OPENID = "photo-eval-manifest-v2"
DEFAULT_NICKNAME = "Photo Eval Dataset v2"
ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True, slots=True)
class SourcePhoto:
    dataset_id: str
    path: Path
    sha256: str
    size_bytes: int
    mime_type: str
    width: int
    height: int
    split: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def load_manifest(manifest_path: Path) -> tuple[dict[str, Any], list[SourcePhoto]]:
    """严格校验清单；哈希、尺寸或路径异常都会在上传前终止。"""
    manifest_path = manifest_path.resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = payload.get("images")
    if not isinstance(rows, list) or not rows:
        raise ValueError("manifest.images 必须是非空数组")
    if payload.get("total_images") != len(rows):
        raise ValueError("manifest.total_images 与 images 数量不一致")

    image_root = (manifest_path.parent / payload.get("image_root", "../..")).resolve()
    seen_ids: set[str] = set()
    photos: list[SourcePhoto] = []
    for row in rows:
        dataset_id = str(row.get("id", "")).strip()
        if not dataset_id or dataset_id in seen_ids:
            raise ValueError(f"图片 ID 为空或重复: {dataset_id!r}")
        seen_ids.add(dataset_id)

        relative_path = Path(str(row.get("path", "")))
        image_path = (image_root / relative_path).resolve()
        if not _inside(image_path, image_root):
            raise ValueError(f"图片路径越出 image_root: {relative_path}")
        if not image_path.is_file():
            raise ValueError(f"图片不存在: {relative_path}")
        if image_path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise ValueError(f"不支持的图片格式: {relative_path}")

        actual_hash = _sha256(image_path)
        if actual_hash != str(row.get("sha256", "")).lower():
            raise ValueError(f"图片哈希变化: {dataset_id}")
        with Image.open(image_path) as image:
            image.verify()
        with Image.open(image_path) as image:
            width, height = image.size
            mime_type = Image.MIME.get(image.format or "")
        mime_type = mime_type or mimetypes.guess_type(image_path.name)[0]
        if not mime_type or not mime_type.startswith("image/"):
            raise ValueError(f"无法识别图片 MIME: {dataset_id}")
        if width != row.get("width") or height != row.get("height"):
            raise ValueError(f"图片尺寸变化: {dataset_id}")

        photos.append(
            SourcePhoto(
                dataset_id=dataset_id,
                path=image_path,
                sha256=actual_hash,
                size_bytes=image_path.stat().st_size,
                mime_type=mime_type,
                width=width,
                height=height,
                split=str(row.get("split", "")),
            )
        )
    return payload, photos


def build_eval_oss_key(user_id: str, photo: SourcePhoto) -> str:
    """使用稳定且隔离的评测 key，便于中断续跑。"""
    mime_extensions = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }
    ext = mime_extensions[photo.mime_type]
    return f"photos/{user_id}/eval/photo-manifest-v2/{photo.sha256}.{ext}"


async def _get_or_create_user(openid: str, nickname: str) -> tuple[User, bool]:
    if not openid.startswith("photo-eval-"):
        raise ValueError("安全限制：测试用户 openid 必须以 'photo-eval-' 开头")
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.wechat_openid == openid))
        user = result.scalar_one_or_none()
        if user is not None:
            return user, False
        user = User(wechat_openid=openid, nickname=nickname)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user, True


async def _existing_photos(
    user_id,
    sources_by_hash: dict[str, SourcePhoto],
) -> tuple[dict[str, Photo], int]:
    """读取已有照片，并按真实文件内容修正可安全覆盖的基础元数据。"""
    metadata_updated = 0
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Photo).where(Photo.user_id == user_id))
        photos = list(result.scalars().all())
        for photo in photos:
            source = sources_by_hash.get(str(photo.hash))
            if source is None:
                continue
            desired = {
                "size_bytes": source.size_bytes,
                "mime_type": source.mime_type,
                "width": source.width,
                "height": source.height,
            }
            changed = False
            for field, value in desired.items():
                if getattr(photo, field) != value:
                    setattr(photo, field, value)
                    changed = True
            if changed:
                metadata_updated += 1
        if metadata_updated:
            await session.commit()
        return {str(photo.hash): photo for photo in photos}, metadata_updated


async def _upload_one(
    user_id: str,
    photo: SourcePhoto,
    semaphore: asyncio.Semaphore,
) -> tuple[SourcePhoto, str, str | None]:
    oss_key = build_eval_oss_key(user_id, photo)
    async with semaphore:
        try:
            meta = await head_object(oss_key)
            if meta is None or meta.size != photo.size_bytes:
                await put_object(oss_key, photo.path.read_bytes(), photo.mime_type)
                meta = await head_object(oss_key)
            if meta is None or meta.size != photo.size_bytes:
                raise RuntimeError("OSS 上传后二次核验失败")
            return photo, oss_key, None
        except Exception as exc:  # noqa: BLE001 - 汇总单张失败，允许续跑
            return photo, oss_key, f"{type(exc).__name__}: {exc}"


async def _insert_uploaded(
    user_id,
    uploaded: list[tuple[SourcePhoto, str, str | None]],
) -> tuple[dict[str, Photo], dict[str, str]]:
    inserted: dict[str, Photo] = {}
    failures: dict[str, str] = {}
    async with AsyncSessionLocal() as session:
        for source, oss_key, error in uploaded:
            if error:
                failures[source.dataset_id] = error
                continue
            photo = Photo(
                user_id=user_id,
                oss_key=oss_key,
                hash=source.sha256,
                size_bytes=source.size_bytes,
                mime_type=source.mime_type,
                width=source.width,
                height=source.height,
                status="pending",
            )
            session.add(photo)
            try:
                await session.commit()
                await session.refresh(photo)
                inserted[source.dataset_id] = photo
            except Exception as exc:  # noqa: BLE001 - 保留其他图片的导入能力
                await session.rollback()
                failures[source.dataset_id] = f"{type(exc).__name__}: {exc}"
    return inserted, failures


async def check_services() -> dict[str, str]:
    """只读检查数据库、Redis 和 OSS，不输出连接串或凭据。"""
    states: dict[str, str] = {}
    try:
        async with engine.connect() as connection:
            await connection.execute(text("select 1"))
        states["database"] = "reachable"
    except Exception as exc:  # noqa: BLE001
        states["database"] = f"unreachable:{type(exc).__name__}"

    try:
        from redis.asyncio import Redis

        from app.config import settings

        redis = Redis.from_url(settings.redis_url)
        try:
            await redis.ping()
        finally:
            await redis.aclose()
        states["redis"] = "reachable"
    except Exception as exc:  # noqa: BLE001
        states["redis"] = f"unreachable:{type(exc).__name__}"

    try:
        await head_object("__photo_eval_connectivity_probe__")
        states["oss"] = "reachable"
    except Exception as exc:  # noqa: BLE001
        states["oss"] = f"unreachable:{type(exc).__name__}"
    return states


async def import_dataset(args: argparse.Namespace) -> dict[str, Any]:
    manifest_payload, sources = load_manifest(args.manifest)
    if args.limit is not None:
        sources = sources[: args.limit]
    manifest_hash = _sha256(args.manifest.resolve())
    if args.dry_run:
        return {
            "mode": "dry-run",
            "manifest_sha256": manifest_hash,
            "validated": len(sources),
            "would_create_or_reuse_user": args.openid,
        }

    states = await check_services()
    required = ["database", "oss"] + ([] if args.no_enqueue else ["redis"])
    unavailable = [name for name in required if states[name] != "reachable"]
    if unavailable:
        raise RuntimeError(f"服务不可用: {', '.join(unavailable)} | {states}")

    source_by_hash = {source.sha256: source for source in sources}
    user, user_created = await _get_or_create_user(args.openid, args.nickname)
    existing_by_hash, metadata_updated = await _existing_photos(user.id, source_by_hash)
    records: dict[str, dict[str, Any]] = {}
    for digest, photo in existing_by_hash.items():
        source = source_by_hash.get(digest)
        if source is not None:
            records[source.dataset_id] = {
                "dataset_id": source.dataset_id,
                "photo_id": str(photo.id),
                "sha256": digest,
                "oss_key": photo.oss_key,
                "status": photo.status,
                "import_action": "reused",
                "queued": False,
            }

    missing = [source for source in sources if source.sha256 not in existing_by_hash]
    semaphore = asyncio.Semaphore(args.upload_concurrency)
    uploaded = await asyncio.gather(
        *[_upload_one(str(user.id), source, semaphore) for source in missing]
    )
    inserted, failures = await _insert_uploaded(user.id, uploaded)
    missing_by_id = {source.dataset_id: source for source in missing}
    for dataset_id, photo in inserted.items():
        source = missing_by_id[dataset_id]
        records[dataset_id] = {
            "dataset_id": dataset_id,
            "photo_id": str(photo.id),
            "sha256": source.sha256,
            "oss_key": photo.oss_key,
            "status": photo.status,
            "import_action": "created",
            "queued": False,
        }

    queue_failures: dict[str, str] = {}
    if not args.no_enqueue:
        from app.workers.tasks import enqueue_process_photo

        for source in sources:
            record = records.get(source.dataset_id)
            if record is None or record["status"] != "pending":
                continue
            try:
                await enqueue_process_photo(record["photo_id"])
                record["queued"] = True
            except Exception as exc:  # noqa: BLE001
                queue_failures[source.dataset_id] = f"{type(exc).__name__}: {exc}"

    ordered = [records[source.dataset_id] for source in sources if source.dataset_id in records]
    result = {
        "version": "1.0.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest_name": manifest_payload.get("name"),
        "manifest_sha256": manifest_hash,
        "user": {
            "id": str(user.id),
            "wechat_openid": user.wechat_openid,
            "nickname": user.nickname,
            "created": user_created,
        },
        "summary": {
            "requested": len(sources),
            "created": sum(r["import_action"] == "created" for r in ordered),
            "reused": sum(r["import_action"] == "reused" for r in ordered),
            "queued": sum(bool(r["queued"]) for r in ordered),
            "metadata_updated": metadata_updated,
            "failed": len(failures) + len(queue_failures),
        },
        "service_check": states,
        "records": ordered,
        "import_failures": failures,
        "queue_failures": queue_failures,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(args.output)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path("tests/eval/photo_manifest.json")
    )
    parser.add_argument("--openid", default=DEFAULT_OPENID)
    parser.add_argument("--nickname", default=DEFAULT_NICKNAME)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/photo-eval/import-map.json")
    )
    parser.add_argument("--upload-concurrency", type=int, default=4, choices=range(1, 9))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true", help="仅检查依赖服务")
    parser.add_argument("--no-enqueue", action="store_true", help="只导入，不送入 Worker")
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    try:
        if args.check:
            result = await check_services()
            print(json.dumps(result, ensure_ascii=False))
            return 0 if all(value == "reachable" for value in result.values()) else 2
        result = await import_dataset(args)
        public_result = {key: value for key, value in result.items() if key != "records"}
        print(json.dumps(public_result, ensure_ascii=False, indent=2))
        return 0 if result.get("summary", {}).get("failed", 0) == 0 else 2
    finally:
        await engine.dispose()


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
