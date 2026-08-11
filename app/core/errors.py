"""统一错误码体系与异常定义.

错误码分段:
- 0:     成功
- -1:    未知错误
- -2:    系统内部错误
- 1xxxx: 认证与用户模块
- 2xxxx: 照片模块
- 3xxxx: AI/Agent 模块
- 4xxxx: Skill/生图模块
- 5xxxx: 搜索模块
- 6xxxx: 限流/配额模块
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# ---------- 错误码注册表 ----------
_REGISTRY: Dict[int, "ErrorCode"] = {}


class ErrorCode:
    """错误码定义."""

    def __init__(self, code: int, msg: str):
        if code in _REGISTRY:
            raise ValueError(f"duplicate error code: {code}")
        self.code = code
        self.msg = msg
        _REGISTRY[code] = self

    def __repr__(self) -> str:
        return f"<ErrorCode {self.code}: {self.msg}>"


# ---------- 通用错误 ----------
SUCCESS = ErrorCode(0, "成功")
UNKNOWN_ERROR = ErrorCode(-1, "未知错误")
SYSTEM_ERROR = ErrorCode(-2, "系统内部错误")
INVALID_PARAMS = ErrorCode(90001, "参数错误")
RATE_LIMITED = ErrorCode(90002, "请求过于频繁，请稍后重试")
SERVICE_DEGRADED = ErrorCode(90003, "服务降级中，请稍后重试")

# ---------- 认证与用户模块 (1xxxx) ----------
AUTH_JWT_EXPIRED = ErrorCode(10001, "登录已过期，请重新登录")
AUTH_JWT_INVALID = ErrorCode(10002, "无效的登录凭证")
AUTH_WECHAT_FAILED = ErrorCode(10003, "微信登录失败")
AUTH_USER_NOT_FOUND = ErrorCode(10004, "用户不存在")
AUTH_PERMISSION_DENIED = ErrorCode(10005, "权限不足")

# ---------- 照片模块 (2xxxx) ----------
PHOTO_NOT_FOUND = ErrorCode(20001, "照片不存在")
PHOTO_UPLOAD_FAILED = ErrorCode(20002, "照片上传失败")
PHOTO_PROCESSING = ErrorCode(20003, "照片正在处理中")
PHOTO_PROCESS_FAILED = ErrorCode(20004, "照片处理失败")
PHOTO_OSS_ERROR = ErrorCode(20005, "存储服务异常")
PHOTO_INVALID_FORMAT = ErrorCode(20006, "不支持的图片格式")

# ---------- AI/Agent 模块 (3xxxx) ----------
AI_LLM_ERROR = ErrorCode(30001, "AI 服务调用失败")
AI_VL_ERROR = ErrorCode(30002, "图片理解失败")
AI_EMBEDDING_ERROR = ErrorCode(30003, "向量编码失败")
AGENT_SESSION_NOT_FOUND = ErrorCode(30004, "对话会话不存在")
AGENT_MAX_STEPS = ErrorCode(30005, "对话轮次过多，请新开对话")
AGENT_TIMEOUT = ErrorCode(30006, "对话响应超时")
AGENT_CLARIFICATION_NEEDED = ErrorCode(30007, "需要进一步澄清需求")

# ---------- Skill/生图模块 (4xxxx) ----------
SKILL_NOT_FOUND = ErrorCode(40001, "Skill 不存在")
SKILL_PERMISSION_DENIED = ErrorCode(40002, "无权使用该 Skill")
GEN_QUOTA_EXCEEDED = ErrorCode(40003, "今日 AI 改造额度已用完")
GEN_FAILED = ErrorCode(40004, "AI 改造失败")
GEN_NOT_FOUND = ErrorCode(40005, "生成记录不存在")
GEN_PROCESSING = ErrorCode(40006, "AI 改造进行中")
GEN_INVALID_PHOTO = ErrorCode(40007, "图片不支持 AI 改造")

# ---------- 搜索模块 (5xxxx) ----------
SEARCH_FAILED = ErrorCode(50001, "搜索失败")
SEARCH_NO_RESULTS = ErrorCode(50002, "未找到匹配的照片")
SEARCH_QUERY_EMPTY = ErrorCode(50003, "搜索内容不能为空")

# ---------- 限流/配额模块 (6xxxx) ----------
RATE_LIMIT_API = ErrorCode(60001, "接口请求频率超限")
RATE_LIMIT_UPLOAD = ErrorCode(60002, "上传频率超限")
RATE_LIMIT_AI = ErrorCode(60003, "AI 调用频率超限")


class ApiError(Exception):
    """业务异常基类.

    所有业务逻辑抛出的异常都应使用此类或其子类，
    由全局 exception_handler 统一捕获并返回标准格式响应.
    """

    def __init__(
        self,
        error_code: ErrorCode,
        message: str = "",
        data: Optional[Any] = None,
        http_status: int = 200,
    ):
        self.error_code = error_code
        self.message = message or error_code.msg
        self.data = data
        self.http_status = http_status
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "errNo": self.error_code.code,
            "errMsg": self.message,
            "data": self.data,
        }

    def __repr__(self) -> str:
        return (
            f"ApiError(code={self.error_code.code}, "
            f"msg={self.message!r}, http_status={self.http_status})"
        )


def get_error_code(code: int) -> Optional[ErrorCode]:
    """根据错误码获取 ErrorCode 实例."""
    return _REGISTRY.get(code)
