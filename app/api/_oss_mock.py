"""Signed, bounded development object storage."""

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from app.config import settings
from app.services.oss import is_mock, mock_path, verify_mock_signature

router = APIRouter(tags=["_mock"], include_in_schema=False)


def _authorize(request: Request, key: str) -> Path:
    if (
        settings.app_env not in {"dev", "test"}
        or not settings.mock_oss_enabled
        or not is_mock()
    ):
        raise HTTPException(404, "Not found")
    try:
        path = mock_path(key)
        query = request.query_params
        valid = verify_mock_signature(
            request.method,
            key,
            int(query["expires"]),
            query.get("mime", ""),
            int(query.get("limit", "0")),
            query["signature"],
        )
    except (ValueError, KeyError):
        raise HTTPException(403, "Invalid object capability") from None
    if not valid:
        raise HTTPException(403, "Invalid object capability")
    return path


@router.put("/_mock/oss/{oss_key:path}")
async def mock_put(oss_key: str, request: Request) -> dict:
    destination = _authorize(request, oss_key)
    limit = min(int(request.query_params["limit"]), settings.mock_oss_max_bytes)
    if request.headers.get("content-type", "") != request.query_params.get("mime"):
        raise HTTPException(400, "Content type mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, "Object exceeds size limit")
                stream.write(chunk)
            if size == 0:
                raise HTTPException(400, "Empty body")
        os.replace(temporary, mock_path(oss_key))
        return {"ok": True, "size": size}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@router.get("/_mock/oss/{oss_key:path}")
async def mock_get(oss_key: str, request: Request) -> FileResponse:
    path = _authorize(request, oss_key)
    if not path.is_file():
        raise HTTPException(404, "Object not found")
    return FileResponse(path, media_type="image/jpeg")


@router.head("/_mock/oss/{oss_key:path}")
async def mock_head(oss_key: str, request: Request) -> Response:
    path = _authorize(request, oss_key)
    if not path.is_file():
        raise HTTPException(404, "Object not found")
    return Response(headers={"Content-Length": str(path.stat().st_size)})
