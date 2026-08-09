"""开发环境下的假 OSS：让客户端 PUT 到 /_mock/oss/... 时也能落盘。

生产环境（配置了真实 OSS_KEY_ID）时这套路由不生效——sign_put_url
返回的是真的阿里云 URL，客户端不会打到本机。
"""
from fastapi import APIRouter, HTTPException, Request, Response

from app.services.oss import is_mock, mock_read_object, mock_write_object

router = APIRouter(tags=["_mock"], include_in_schema=False)


@router.put("/_mock/oss/{oss_key:path}")
async def mock_put(oss_key: str, request: Request) -> dict:
    if not is_mock():
        raise HTTPException(status_code=404, detail="Not in mock mode")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")

    # 用 BytesIO 让 shutil.copyfileobj 有输入源
    import io

    meta = mock_write_object(oss_key, io.BytesIO(body))
    return {"ok": True, "size": meta.size, "etag": meta.etag}


@router.get("/_mock/oss/{oss_key:path}")
async def mock_get(oss_key: str) -> Response:
    if not is_mock():
        raise HTTPException(status_code=404, detail="Not in mock mode")

    data = mock_read_object(oss_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Object not found")
    return Response(content=data, media_type="image/jpeg")


@router.head("/_mock/oss/{oss_key:path}")
async def mock_head(oss_key: str) -> Response:
    if not is_mock():
        raise HTTPException(status_code=404, detail="Not in mock mode")
    data = mock_read_object(oss_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Object not found")
    return Response(status_code=200, headers={"Content-Length": str(len(data))})
