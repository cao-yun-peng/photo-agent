"""LLM输出JSON解析兜底工具.

参考 llm-rag-server 生产级解析容错设计:
- 三级解析容错: JSON → 正则提取 → 默认值
- 自动剥离markdown代码块
- 兼容dict/list/str/bytes/None多种输入
- 解析失败不抛异常，返回原值或默认值
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, TypeVar

T = TypeVar("T")


def parse_json_field(value: Any) -> Any:
    """解析JSON字段，兼容多种输入类型，失败返回原值不抛异常.

    处理流程:
    1. None → None
    2. dict/list → 直接返回
    3. bytes/bytearray → utf-8解码(errors="ignore")
    4. str → 先strip()，空串返回None，尝试json.loads，失败返回原字符串
    """
    if value is None:
        return None

    if isinstance(value, (dict, list)):
        return value

    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return value

    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None

        # 尝试剥离markdown代码块
        s = _strip_markdown_code_block(s)

        # 尝试提取JSON片段（处理前后夹杂文本的情况）
        s = _extract_json_fragment(s)

        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            pass

        # 尝试宽松解析：处理未转义引号、尾逗号等常见问题
        try:
            return _lenient_json_parse(s)
        except Exception:
            pass

        # 全部失败返回原字符串
        return value

    return value


def parse_json_or_default(value: Any, default: T = None) -> Any | T:
    """解析JSON，失败返回指定默认值."""
    result = parse_json_field(value)
    if isinstance(result, str):
        # 解析失败返回了原字符串，说明没解析成功
        return default
    return result if result is not None else default


def parse_as_dict(value: Any) -> Dict[str, Any]:
    """解析为字典，失败返回空字典."""
    result = parse_json_field(value)
    return result if isinstance(result, dict) else {}


def parse_as_list(value: Any) -> List[Any]:
    """解析为列表，失败返回空列表."""
    result = parse_json_field(value)
    return result if isinstance(result, list) else []


def parse_as_list_of_dict(value: Any) -> List[Dict[str, Any]]:
    """解析为字典列表，非字典元素过滤掉."""
    result = parse_as_list(value)
    return [item for item in result if isinstance(item, dict)]


def _strip_markdown_code_block(s: str) -> str:
    """剥离markdown代码块标记 ```json ... ```."""
    s = s.strip()
    if s.startswith("```"):
        # 移除开头的 ```language 标记
        s = re.sub(r"^```\w*\n?", "", s)
        # 移除结尾的 ```
        s = re.sub(r"\n?```\s*$", "", s).strip()
    return s


def _extract_json_fragment(s: str) -> str:
    """从文本中提取JSON片段（处理LLM输出前后夹杂解释文本的情况）."""
    # 尝试找最外层的 {...}
    brace_start = s.find("{")
    brace_end = s.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        candidate = s[brace_start : brace_end + 1]
        # 验证括号是否平衡（简单检查）
        if candidate.count("{") == candidate.count("}"):
            return candidate

    # 尝试找最外层的 [...]
    bracket_start = s.find("[")
    bracket_end = s.rfind("]")
    if bracket_start != -1 and bracket_end != -1 and bracket_end > bracket_start:
        candidate = s[bracket_start : bracket_end + 1]
        if candidate.count("[") == candidate.count("]"):
            return candidate

    return s


def _lenient_json_parse(s: str) -> Any:
    """宽松JSON解析，修复一些LLM常见的输出问题."""
    # 1. 移除尾逗号 (trailing commas)
    s = re.sub(r",\s*([}\]])", r"\1", s)

    # 2. 处理单引号 (LLM有时用单引号代替双引号)
    # 简单处理：将key位置的单引号替换为双引号
    def replace_single_quotes(match: re.Match) -> str:
        return '"' + match.group(1) + '":'

    s = re.sub(r"'([^']+)'\s*:", replace_single_quotes, s)

    # 3. 尝试再次解析
    return json.loads(s)


def extract_json_field_by_regex(
    raw_content: str,
    field_name: str,
    default: str = "",
) -> str:
    """用正则从LLM原始输出中提取单个字段值（JSON解析失败后的兜底）.

    支持:
    - "key": "value"
    - 'key': 'value'
    - key: "value"
    """
    patterns = [
        # 双引号: "key": "value"
        fr'"{field_name}"\s*:\s*"([^"]*)"',
        # 单引号: 'key': 'value'
        fr"'{field_name}'\s*:\s*'([^']*)'",
        # 无引号key: key: "value"
        fr'{field_name}\s*:\s*"([^"]*)"',
        # 布尔值: "key": true/false
        fr'"{field_name}"\s*:\s*(true|false)',
    ]

    for pattern in patterns:
        match = re.search(pattern, raw_content, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()

    return default


def extract_bool_field_by_regex(
    raw_content: str,
    field_name: str,
    default: bool = False,
) -> bool:
    """用正则提取布尔字段."""
    value = extract_json_field_by_regex(raw_content, field_name, "")
    if not value:
        return default
    return value.lower() in ("true", "yes", "1", "是")


def validate_string_whitelist(
    value: str,
    whitelist: set[str],
    default: str,
) -> str:
    """白名单校验字符串值，不在白名单返回默认值."""
    return value if value in whitelist else default


def truncate_text(text: str, max_length: int, suffix: str = "...") -> str:
    """截断文本到指定长度."""
    if not text or len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix
