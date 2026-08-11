"""安全AST条件表达式求值器.

参考 llm-rag-server 生产级实现，定制适用于photo-agent场景的语义函数：
- 点路径自动安全取值 (a.b.c)，不存在返回默认值，不抛KeyError
- exists_path(path): 判断路径是否存在
- is_blank(value): 判断值是否为空（None/空字符串）
- 受限AST求值，只允许白名单操作，无代码注入风险

典型用途：
- Skill触发条件配置（"photo.scene == 'beach' and not is_blank(photo.location)"）
- 搜索结果过滤规则
- Agent决策条件表达式（运营可配置，无需改代码）
"""
from __future__ import annotations

import ast
import re
from typing import Any, Callable, Dict, List, Mapping, Optional

_MISSING = object()

_DOT_PATH_PATTERN = re.compile(
    r"\b(state|photo|context|user|session)\.(?:[A-Za-z_]\w*|\d+)(?:\.(?:[A-Za-z_]\w*|\d+))*"
)


# ---------------------------------------------------------------------------
# 基础语义函数
# ---------------------------------------------------------------------------

def path_get(data: Any, path: str, default: Any = "") -> Any:
    """按 `a.b.c` 点路径从 dict/list 中安全取值。

    任何环节不存在都返回default，永远不会抛KeyError/IndexError/TypeError。
    支持数字索引（list访问），如 items.0.name。
    """
    if data is None:
        return default
    raw = str(path or "").strip()
    if not raw:
        return default
    cur = data
    for seg in raw.split("."):
        if isinstance(cur, Mapping):
            if seg in cur:
                cur = cur.get(seg)
                continue
            return default
        if isinstance(cur, (list, tuple)):
            if not seg.isdigit():
                return default
            idx = int(seg)
            if idx < 0 or idx >= len(cur):
                return default
            cur = cur[idx]
            continue
        if hasattr(cur, seg):
            cur = getattr(cur, seg)
            continue
        return default
    return cur


def exists_path(data: Any, path: str) -> bool:
    """判断指定路径是否存在（区分"不存在"和"值为None/空串"）。

    与 path_get 不同，exists_path 只检查key是否存在，不关心值是什么。
    """
    return path_get(data, path, _MISSING) is not _MISSING


def is_blank(value: Any) -> bool:
    """判断值是否为空：None、空字符串、空列表、空字典都算blank。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def is_not_blank(value: Any) -> bool:
    """is_blank的反义，方便写条件。"""
    return not is_blank(value)


def contains(value: Any, item: Any) -> bool:
    """安全的包含判断，value不是容器时返回False而非抛异常。"""
    try:
        return item in value
    except TypeError:
        return False


def starts_with(value: str, prefix: str) -> bool:
    """安全的前缀匹配。"""
    if not isinstance(value, str):
        return False
    return value.startswith(prefix)


def ends_with(value: str, suffix: str) -> bool:
    """安全的后缀匹配。"""
    if not isinstance(value, str):
        return False
    return value.endswith(suffix)


def length(value: Any) -> int:
    """安全的长度获取，非容器返回0。"""
    try:
        return len(value)
    except TypeError:
        return 0


# ---------------------------------------------------------------------------
# 安全函数白名单
# ---------------------------------------------------------------------------

_SAFE_FUNCTIONS: Dict[str, Callable[..., Any]] = {
    # Python内置
    "bool": bool,
    "int": int,
    "str": str,
    "float": float,
    "len": length,
    "max": max,
    "min": min,
    "abs": abs,
    "round": round,
    "list": list,
    "dict": dict,
    "set": set,
    # 语义函数
    "path_get": path_get,
    "exists_path": exists_path,
    "is_blank": is_blank,
    "is_not_blank": is_not_blank,
    "contains": contains,
    "starts_with": starts_with,
    "ends_with": ends_with,
    "length": length,
}
_SAFE_FUNCTION_SET = set(_SAFE_FUNCTIONS.values())


# ---------------------------------------------------------------------------
# 点路径重写（a.b.c -> path_get(a, "b.c", "")）
# ---------------------------------------------------------------------------

def _rewrite_dot_paths(expression: str) -> str:
    """将点路径表达式自动改写为path_get调用，避免KeyError。

    例如:
      photo.scene == 'beach'
      → path_get(photo, "scene", "") == 'beach'

      state.search_attempts >= 2
      → path_get(state, "search_attempts", "") >= 2
    """
    text = str(expression or "")
    out: list[str] = []
    i = 0
    in_single = False
    in_double = False

    while i < len(text):
        ch = text[i]

        # 跳过字符串内容（字符串里的点不处理）
        if ch == "'" and not in_double:
            in_single = not in_single
            out.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
            i += 1
            continue
        if in_single or in_double:
            out.append(ch)
            i += 1
            continue

        # 尝试匹配点路径
        m = _DOT_PATH_PATTERN.match(text, i)
        if not m:
            out.append(ch)
            i += 1
            continue

        matched = m.group(0)
        j = m.end()

        # 跳过空白
        while j < len(text) and text[j].isspace():
            j += 1

        # 兼容保留旧语法 .get( 不重写
        if j < len(text) and text[j] == "(":
            out.append(matched)
            i = m.end()
            continue

        # 拆分root和path
        root, _, path = matched.partition(".")
        out.append(f'path_get({root}, "{path}", "")')
        i = m.end()

    return "".join(out)


# ---------------------------------------------------------------------------
# 受限AST求值核心
# ---------------------------------------------------------------------------

def _safe_eval(expression: str, context: Dict[str, Any]) -> Any:
    """受限AST求值：只允许白名单内的操作。

    支持:
    - 常量（字符串/数字/布尔/None/列表/字典/元组）
    - 变量（从context取）
    - 布尔运算 and/or/not
    - 比较运算 ==/!=/>/>=/</<=/in/not in/is/is not
    - 下标访问 []
    - 白名单函数调用
    - 容器字面量 [] () {}
    """
    tree = ast.parse(expression, mode="eval")

    def _eval(node: ast.AST) -> Any:
        # 表达式根节点
        if isinstance(node, ast.Expression):
            return _eval(node.body)

        # 常量
        if isinstance(node, ast.Constant):
            return node.value

        # 变量（从context取，或取安全函数）
        if isinstance(node, ast.Name):
            name = node.id
            if name in context:
                return context[name]
            if name in _SAFE_FUNCTIONS:
                return _SAFE_FUNCTIONS[name]
            if name == "True":
                return True
            if name == "False":
                return False
            if name == "None":
                return None
            raise ValueError(f"不支持的变量: {name}")

        # 布尔运算 and/or
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                for v in node.values:
                    val = _eval(v)
                    if not val:
                        return val
                return val
            if isinstance(node.op, ast.Or):
                for v in node.values:
                    val = _eval(v)
                    if val:
                        return val
                return val
            raise ValueError(f"不支持的布尔操作: {type(node.op)}")

        # 一元运算 not
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return not _eval(node.operand)
            if isinstance(node.op, (ast.UAdd, ast.USub)):
                operand = _eval(node.operand)
                return +operand if isinstance(node.op, ast.UAdd) else -operand
            raise ValueError(f"不支持的一元操作: {type(node.op)}")

        # 比较运算（支持链式比较 a < b < c）
        if isinstance(node, ast.Compare):
            left = _eval(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = _eval(comp)
                if isinstance(op, ast.Eq):
                    ok = left == right
                elif isinstance(op, ast.NotEq):
                    ok = left != right
                elif isinstance(op, ast.Gt):
                    ok = left > right
                elif isinstance(op, ast.GtE):
                    ok = left >= right
                elif isinstance(op, ast.Lt):
                    ok = left < right
                elif isinstance(op, ast.LtE):
                    ok = left <= right
                elif isinstance(op, ast.In):
                    try:
                        ok = left in right
                    except TypeError:
                        ok = False
                elif isinstance(op, ast.NotIn):
                    try:
                        ok = left not in right
                    except TypeError:
                        ok = True
                elif isinstance(op, ast.Is):
                    ok = left is right
                elif isinstance(op, ast.IsNot):
                    ok = left is not right
                else:
                    raise ValueError(f"不支持的比较操作: {type(op)}")
                if not ok:
                    return False
                left = right
            return True

        # 二元算术运算
        if isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right if right != 0 else 0
            if isinstance(node.op, ast.FloorDiv):
                return left // right if right != 0 else 0
            if isinstance(node.op, ast.Mod):
                return left % right if right != 0 else 0
            raise ValueError(f"不支持的二元操作: {type(node.op)}")

        # 下标访问 obj[key]
        if isinstance(node, ast.Subscript):
            target = _eval(node.value)
            key = _eval(node.slice)
            try:
                return target[key]
            except (KeyError, IndexError, TypeError):
                return None

        # 函数调用（仅白名单内）
        if isinstance(node, ast.Call):
            fn = _eval(node.func)
            args = [_eval(a) for a in node.args]
            kwargs = {kw.arg: _eval(kw.value) for kw in node.keywords if kw.arg}
            if fn in _SAFE_FUNCTION_SET:
                return fn(*args, **kwargs)
            # 允许dict.get()方法
            if getattr(fn, "__name__", "") == "get" and isinstance(getattr(fn, "__self__", None), dict):
                return fn(*args, **kwargs)
            raise ValueError(f"不支持的函数调用: {getattr(fn, '__name__', fn)}")

        # 属性访问（只允许dict.get）
        if isinstance(node, ast.Attribute):
            base = _eval(node.value)
            if isinstance(base, dict) and node.attr == "get":
                return base.get
            if isinstance(base, (list, str)) and node.attr in ("count", "index", "find", "strip"):
                return getattr(base, node.attr)
            raise ValueError(f"不支持的属性访问: .{node.attr}")

        # 容器字面量
        if isinstance(node, ast.List):
            return [_eval(e) for e in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(_eval(e) for e in node.elts)
        if isinstance(node, ast.Dict):
            return {_eval(k): _eval(v) for k, v in zip(node.keys, node.values) if k is not None}
        if isinstance(node, ast.Set):
            return {_eval(e) for e in node.elts}

        # 条件表达式 a if cond else b
        if isinstance(node, ast.IfExp):
            test = _eval(node.test)
            return _eval(node.body) if test else _eval(node.orelse)

        raise ValueError(f"不支持的表达式节点: {type(node).__name__}")

    return _eval(tree)


# ---------------------------------------------------------------------------
# 对外API
# ---------------------------------------------------------------------------

def evaluate_condition(
    condition: str,
    context: Optional[Dict[str, Any]] = None,
    *,
    default: bool = False,
    logger: Optional[Any] = None,
) -> bool:
    """统一条件评估入口。

    Args:
        condition: 条件表达式字符串，如:
            - "photo.scene == 'beach'"
            - "exists_path(photo, 'location') and not is_blank(photo.location)"
            - "state.search_attempts >= 2"
            - "" 或 "default" → 返回True（默认通过）
        context: 变量上下文，如 {"photo": {...}, "state": {...}}
        default: 表达式求值失败时的返回值（默认False，安全优先）
        logger: 可选logger，求值失败时记录warning

    Returns:
        条件求值结果，布尔值

    示例:
        # 判断照片是否是海景且有地点信息
        evaluate_condition(
            "photo.scene == 'sea' and not is_blank(photo.location)",
            context={"photo": {"scene": "sea", "location": "三亚"}}
        )
        # → True

        # 判断是否需要触发澄清（搜索次数≥2）
        evaluate_condition(
            "state.search_attempts >= 2",
            context={"state": {"search_attempts": 2}}
        )
        # → True

        # 不存在的key不会报错，返回default
        evaluate_condition(
            "photo.nonexistent_field == 'x'",
            context={"photo": {}}
        )
        # → False (不存在的路径返回""，不等于'x')
    """
    raw = str(condition or "").strip()
    if not raw or raw.lower() == "default" or raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False

    ctx = context or {}
    try:
        rewritten = _rewrite_dot_paths(raw)
        result = _safe_eval(rewritten, ctx)
        return bool(result)
    except Exception as e:
        if logger is not None:
            logger.warning(
                "condition evaluation failed: %s | condition=%r",
                e, raw,
            )
        return default


def evaluate_expression(
    expression: str,
    context: Optional[Dict[str, Any]] = None,
    *,
    default: Any = None,
    logger: Optional[Any] = None,
) -> Any:
    """通用表达式求值，返回任意类型值（evaluate_condition返回bool）。"""
    raw = str(expression or "").strip()
    if not raw:
        return default

    ctx = context or {}
    try:
        rewritten = _rewrite_dot_paths(raw)
        return _safe_eval(rewritten, ctx)
    except Exception as e:
        if logger is not None:
            logger.warning(
                "expression evaluation failed: %s | expr=%r",
                e, raw,
            )
        return default
