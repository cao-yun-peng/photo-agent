"""微信小程序 jscode2session 换 openid."""
import httpx

from app.config import settings


WECHAT_JSCODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"


class WeChatError(Exception):
    """微信接口异常."""


async def code2session(code: str) -> dict:
    """
    用小程序 code 换取 openid / session_key。
    返回原始 dict，至少包含 openid。
    dev 环境下若未配置 AppID，则返回 mock 数据便于本地联调。
    """
    has_wechat_config = bool(
        settings.wechat_appid
        and settings.wechat_appid != "wx_your_appid"
        and settings.wechat_secret
        and settings.wechat_secret != "your_secret"
    )
    if settings.app_env == "dev" and not has_wechat_config:
        # 本地开发：伪造一个 openid，避免必须先配置微信才能起服务
        return {"openid": f"dev_openid_{code}", "mock": True}
    if not has_wechat_config:
        raise WeChatError("WeChat login is not configured")

    params = {
        "appid": settings.wechat_appid,
        "secret": settings.wechat_secret,
        "js_code": code,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(WECHAT_JSCODE2SESSION_URL, params=params)
    data = resp.json()
    if "errcode" in data and data["errcode"] != 0:
        raise WeChatError(f"WeChat error {data['errcode']}: {data.get('errmsg')}")
    if "openid" not in data:
        raise WeChatError(f"Unexpected response: {data}")
    return data
