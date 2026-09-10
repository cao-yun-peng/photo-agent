"""Request-local calendar semantics shared by parsing, SQL and prefetch."""

from contextvars import ContextVar
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from app.config import settings

_zone: ContextVar[str | None] = ContextVar("search_timezone", default=None)


def current_timezone() -> str:
    return _zone.get() or settings.search_default_timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_today(timezone_name: str | None = None, clock=None) -> date:
    return (
        (clock or utc_now)()
        .astimezone(ZoneInfo(timezone_name or current_timezone()))
        .date()
    )


def date_bounds(start: date | None, end: date | None, timezone_name: str | None = None):
    zone = ZoneInfo(timezone_name or current_timezone())
    return (
        datetime.combine(start, time.min, zone).astimezone(timezone.utc)
        if start
        else None,
        datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(
            timezone.utc
        )
        if end
        else None,
    )


class SearchTimezoneMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.responses import JSONResponse

        name = (
            dict(scope.get("headers", [])).get(b"x-timezone", b"").decode("latin1")
            or settings.search_default_timezone
        )
        try:
            ZoneInfo(name)
        except (ValueError, KeyError):
            return await JSONResponse(
                {"detail": "Invalid X-Timezone"}, status_code=422
            )(scope, receive, send)
        token = _zone.set(name)
        try:
            await self.app(scope, receive, send)
        finally:
            _zone.reset(token)
