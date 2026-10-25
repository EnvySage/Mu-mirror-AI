"""The single registry for graph_v2 read tools."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any

from agent_data.records import CONTENT_TYPES, MOODS
from agent_data.todos import TODO_STATUSES


@dataclass(frozen=True, slots=True)
class ReadToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    effect: str = "read"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "effect": self.effect,
        }


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_DATE_PROPERTIES = {
    "date": {"type": "string", "format": "date", "description": "单个本地日期，YYYY-MM-DD"},
    "date_from": {"type": "string", "format": "date", "description": "包含的起始本地日期"},
    "date_to": {"type": "string", "format": "date", "description": "包含的结束本地日期"},
    "days": {"type": "integer", "minimum": 1, "maximum": 36500},
}

_PAGE_PROPERTIES = {
    "offset": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
}


READ_TOOL_REGISTRY: dict[str, ReadToolSpec] = {
    "search_records": ReadToolSpec(
        "search_records", "按日期、类型、情绪和字面短关键词查询已授权日记片段，可按 record_id 或 offset 查询。",
        _schema({
            **_DATE_PROPERTIES,
            "moods": {"type": "array", "items": {"type": "string", "enum": sorted(MOODS)}, "maxItems": 13},
            "content_type": {"type": "string", "enum": sorted(CONTENT_TYPES)},
            "query": {"type": "string", "maxLength": 256},
            "record_id": {"type": "integer", "minimum": 1},
            **_PAGE_PROPERTIES,
        }),
    ),
    "read_record": ReadToolSpec(
        "read_record", "按原顺序分页读取一个记录的已授权完整片段，按 next_offset 继续；不会读取未授权原文。",
        _schema({"record_id": {"type": "integer", "minimum": 1}, **_PAGE_PROPERTIES}, ("record_id",)),
    ),
    "list_todos": ReadToolSpec(
        "list_todos", "读取单独获得待办授权的任务，可按状态过滤并分页。",
        _schema({"status": {"type": "string", "enum": list(TODO_STATUSES)}, **_PAGE_PROPERTIES}),
    ),
    "get_stats": ReadToolSpec(
        "get_stats", "统计时间窗内记录数与情绪分布，并列出当前授权待办状态。",
        _schema({key: value for key, value in _DATE_PROPERTIES.items()}),
    ),
    "get_profile": ReadToolSpec(
        "get_profile", "读取有效画像快照；可按月份回溯，manual 优先于 monthly。",
        _schema({"month": {"type": "string", "pattern": "^\\d{4}-(0[1-9]|1[0-2])$"}}),
    ),
    "get_coverage": ReadToolSpec(
        "get_coverage", "统计主题命中的最早日期、最近日期和不同日期数。",
        _schema({"keyword": {"type": "string", "minLength": 1, "maxLength": 256}}, ("keyword",)),
    ),
    "get_glossary": ReadToolSpec(
        "get_glossary", "读取与意图上下文相同排序的 confirmed 词典前30项。", _schema({}),
    ),
    "compare_snapshots": ReadToolSpec(
        "compare_snapshots", "比较当前用户两份有效画像快照的五个维度。",
        _schema({
            "first_id": {"type": "integer", "minimum": 1},
            "second_id": {"type": "integer", "minimum": 1},
        }, ("first_id", "second_id")),
    ),
    "find_item": ReadToolSpec(
        "find_item", "按名称、描述、词典别名、confirmed 向量和类型日期查找文件元信息。",
        _schema({
            "query": {"type": "string", "maxLength": 256},
            "category": {"type": "string", "enum": sorted(CONTENT_TYPES)},
            "date_from": _DATE_PROPERTIES["date_from"],
            "date_to": _DATE_PROPERTIES["date_to"],
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        }),
    ),
    "recall_item": ReadToolSpec(
        "recall_item", "无 query 时按原顺序分页读取已确认正文，按 next_offset 继续；query 只返回向量相关摘录。缺少文件ID时由本轮runner补接。",
        _schema({
            "vault_item_id": {"type": "integer", "minimum": 1},
            "query": {"type": "string", "maxLength": 256},
            **_PAGE_PROPERTIES,
        }),
    ),
    "save_item": ReadToolSpec(
        "save_item", "兼容确认文件已保存的只读查询，返回文件元信息，不会创建或修改文件。",
        _schema({"vault_item_id": {"type": "integer", "minimum": 1}}, ("vault_item_id",)),
    ),
}


def validate_read_tool_args(name: str, args: Any) -> dict[str, Any]:
    """Validate the model-visible schema before any repository is accessed."""
    _validate_schema(args, READ_TOOL_REGISTRY[name].parameters, "args")
    return dict(args)


def _validate_schema(value: Any, schema: dict[str, Any], path: str) -> None:
    kind = schema["type"]
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str),
             "integer": isinstance(value, int) and not isinstance(value, bool)}[kind]
    if not valid:
        raise ValueError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} has an unsupported value")
    if kind == "object":
        properties = schema["properties"]
        unknown = set(value) - set(properties)
        if unknown:
            raise ValueError(f"unsupported argument(s): {', '.join(sorted(map(str, unknown)))}")
        if set(schema["required"]) - set(value):
            raise ValueError(f"{path} is missing required arguments")
        for key, child in value.items():
            _validate_schema(child, properties[key], f"{path}.{key}")
    elif kind == "array":
        if len(value) > schema.get("maxItems", len(value)):
            raise ValueError(f"{path} has too many items")
        for index, child in enumerate(value):
            _validate_schema(child, schema["items"], f"{path}[{index}]")
    elif kind == "integer":
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise ValueError(f"{path} is out of bounds")
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
            raise ValueError(f"{path} has an invalid length")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ValueError(f"{path} has an invalid format")
        if schema.get("format") == "date":
            try:
                valid_date = date.fromisoformat(value).isoformat() == value
            except ValueError:
                valid_date = False
            if not valid_date:
                raise ValueError(f"{path} must be a valid YYYY-MM-DD date")


def get_read_tool_specs(allowed_names: set[str] | None = None) -> list[dict[str, Any]]:
    """Return model-visible read tools, filtered by an optional per-run allow-list."""
    return [
        spec.as_dict()
        for name, spec in READ_TOOL_REGISTRY.items()
        if allowed_names is None or name in allowed_names
    ]
