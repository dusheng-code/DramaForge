"""职责：把 Pydantic 模型（含 `list[Model]` 形式）转成 OpenAI 兼容端点的结构化输出 schema，并把模型回包载荷校验成模型实例（§8）。
输入：Pydantic 模型类 / `list[Model]` 泛型别名 / 模型返回的 dict、list 或 JSON 文本。
输出：纯 JSON Schema dict（`response_format.json_schema.schema` 部分）、完整 `json_schema` response_format、校验后的模型实例。

副作用：无。只做 schema 生成与数据校验，不发网络、不写磁盘、不记日志正文。
"""

from __future__ import annotations

import json
import re
from typing import Any, get_args, get_origin

from pydantic import BaseModel, TypeAdapter, ValidationError

from .client import LLMError

#：标量类型在 function calling 里的可读函数名（避免出现 `str` 这种过于通用的名字）
_SCALAR_NAMES: dict[Any, str] = {
    str: "text",
    int: "integer",
    float: "number",
    bool: "boolean",
}

_SCALAR_SCHEMAS: dict[Any, dict[str, Any]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
}

_SNAKE_RE = re.compile(r"(?<!^)(?=[A-Z])")
_NAME_SAFE_RE = re.compile(r"[^0-9a-zA-Z_]+")


def _snake(name: str) -> str:
    """CamelCase → snake_case 并清掉非法字符。无副作用。"""
    raw = _SNAKE_RE.sub("_", name or "").lower()
    cleaned = _NAME_SAFE_RE.sub("_", raw).strip("_")
    return cleaned or "structured_output"


def schema_name(model_cls: Any) -> str:
    """给模型起一个符合 function calling 命名要求的名字（仅字母/数字/下划线）。无副作用。"""
    origin = get_origin(model_cls)
    if origin in (list, tuple):
        args = get_args(model_cls)
        inner = schema_name(args[0]) if args else "item"
        return f"{inner}_list"
    if isinstance(model_cls, type) and issubclass(model_cls, BaseModel):
        return _snake(model_cls.__name__)
    for scalar, label in _SCALAR_NAMES.items():
        if model_cls is scalar:
            return label
    return _snake(str(model_cls))


def _base_schema(model_cls: Any) -> dict[str, Any]:
    """单层 schema：模型类走 `model_json_schema`，标量走内置映射。无副作用。"""
    if isinstance(model_cls, type) and issubclass(model_cls, BaseModel):
        return dict(model_cls.model_json_schema())
    for scalar, schema in _SCALAR_SCHEMAS.items():
        if model_cls is scalar:
            return dict(schema)
    # 未知类型（如 Any）退化为「任意 JSON」，避免生成非法 schema
    return {}


def _inline_refs(node: Any, defs: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    """把 pydantic 生成 `$ref/$defs` 就地内联（部分 OpenAI 兼容端点不认 `$ref`）。无副作用。

    本项目契约模型均为扁平结构、无自引用；万一出现递归引用，按「任意对象」截断并放开
    additionalProperties，保证 schema 合法而不是无限展开。
    """
    if isinstance(node, list):
        return [_inline_refs(item, defs, seen) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            return {"type": "object", "additionalProperties": True}
        target = defs.get(name) or {}
        merged = {k: v for k, v in node.items() if k != "$ref"}
        return _inline_refs({**target, **merged}, defs, (*seen, name))
    out: dict[str, Any] = {k: _inline_refs(v, defs, seen) for k, v in node.items() if k != "$defs"}
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out.setdefault("additionalProperties", False)
    return out


def response_schema(model_cls: Any) -> dict[str, Any]:
    """模型类或 `list[Model]` → 纯 JSON Schema（不含 response_format 外壳）。无副作用。

    只声明真实必填字段：带默认值的字段不进 `required`，避免 schema 比 Pydantic 更严导致
    合法输出被判违规（严格模式下由提示词与 `parse_into` 共同兜底）。
    """
    origin = get_origin(model_cls)
    if origin in (list, tuple):
        args = get_args(model_cls)
        if not args:  # 裸 list：退化成任意数组
            return {"type": "array", "items": {}}
        return {"type": "array", "items": response_schema(args[0])}
    raw = _base_schema(model_cls)
    defs = raw.get("$defs") or {}
    schema = _inline_refs(raw, defs)
    # 顶层对象也要封口，防止模型塞入契约外字段
    if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
        schema.setdefault("additionalProperties", False)
    return schema or {"type": "object", "additionalProperties": True}


def json_schema_response_format(model_cls: Any, name: str | None = None) -> dict[str, Any]:
    """组装 `response_format={"type":"json_schema", ...}` 的完整值。无副作用。"""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name or schema_name(model_cls),
            "strict": True,
            "schema": response_schema(model_cls),
        },
    }


def parse_into(model_cls: Any, payload: Any) -> Any:
    """把 dict / list（或 JSON 文本）载荷校验成模型；失败抛不可重试的 LLMError。无副作用。

    支持 `BaseModel` 子类与 `list[Model]` 泛型别名；`str` 等标量类型直接透传校验。
    """
    data = payload
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", "replace")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as exc:
            raise LLMError("LLM 结构化载荷不是合法 JSON，无法校验（不可重试）") from exc
    try:
        return TypeAdapter(model_cls).validate_python(data)
    except ValidationError as exc:
        raise LLMError(f"LLM 输出不符合 {schema_name(model_cls)} 契约：{_first_error(exc)}（不可重试）") from exc
    except (TypeError, ValueError) as exc:
        raise LLMError(f"LLM 输出无法校验为 {schema_name(model_cls)}：{type(exc).__name__}") from exc


def _first_error(exc: ValidationError) -> str:
    """取第一条校验错误的位置与原因，避免把整段输入回显进日志。无副作用。"""
    errors = exc.errors()
    if not errors:
        return "未知校验错误"
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
    return f"{loc}: {first.get('msg', '')}"
