"""The single registry for graph_v2 read tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


READ_TOOL_REGISTRY: dict[str, ReadToolSpec] = {
    "search_records": ReadToolSpec(
        "search_records", "按日期、类型、情绪和短关键词查询已授权日记片段。",
        _schema({
            **_DATE_PROPERTIES,
            "moods": {"type": "array", "items": {"type": "string"}, "maxItems": 13},
            "content_type": {"type": "string"},
            "query": {"type": "string", "maxLength": 256},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        }),
    ),
    "get_stats": ReadToolSpec(
        "get_stats", "统计时间窗内记录数与情绪分布，并列出当前授权待办状态。",
        _schema({key: value for key, value in _DATE_PROPERTIES.items()}),
    ),
    "get_profile": ReadToolSpec(
        "get_profile", "读取有效画像快照；可按月份回溯，manual 优先于 monthly。",
        _schema({"month": {"type": "string", "pattern": "^\\d{4}-\\d{2}$"}}),
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
            "category": {"type": "string"},
            "date_from": _DATE_PROPERTIES["date_from"],
            "date_to": _DATE_PROPERTIES["date_to"],
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        }),
    ),
    "recall_item": ReadToolSpec(
        "recall_item", "读取一个文件已确认的正文片段；缺少文件ID时由本轮runner补接。",
        _schema({
            "vault_item_id": {"type": "integer", "minimum": 1},
            "query": {"type": "string", "maxLength": 256},
        }),
    ),
    "save_item": ReadToolSpec(
        "save_item", "兼容确认文件已保存的只读查询，返回文件元信息，不会创建或修改文件。",
        _schema({"vault_item_id": {"type": "integer", "minimum": 1}}, ("vault_item_id",)),
    ),
}


def get_read_tool_specs(allowed_names: set[str] | None = None) -> list[dict[str, Any]]:
    """Return model-visible read tools, filtered by an optional per-run allow-list."""
    return [
        spec.as_dict()
        for name, spec in READ_TOOL_REGISTRY.items()
        if allowed_names is None or name in allowed_names
    ]
