"""Web Phase 0 的后端边界测试。"""

import pytest

from app.config import settings
from app.services.wechat import WeChatError, code2session


@pytest.mark.asyncio
async def test_mock_wechat_login_is_available_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(settings, "wechat_appid", "wx_your_appid")
    monkeypatch.setattr(settings, "wechat_secret", "your_secret")

    session = await code2session("web-dev")

    assert session == {"openid": "dev_openid_web-dev", "mock": True}


@pytest.mark.asyncio
async def test_mock_wechat_login_is_rejected_outside_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "wechat_appid", "wx_your_appid")
    monkeypatch.setattr(settings, "wechat_secret", "your_secret")

    with pytest.raises(WeChatError, match="not configured"):
        await code2session("web-dev")
