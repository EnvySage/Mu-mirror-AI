"""prompts 统一加载与填充

模板用 {placeholder} 占位。提供按名加载和 .format 安全填充。
"""

from pathlib import Path

from config import CONFIG

PROMPT_DIR = Path(__file__).parent / "prompts"

# 逻辑名 → 文件名
_FILENAME = {
    "classify": "classify",
    "classify_single": "classify-single",
    "intent": "intent",
    "chat": "chat",
    "profile": "profile",
    "inspiration": "inspiration",
    "extract_terms": "extract_terms",
    "plan_tools": "plan_tools",
}

# 代码内占位符 → 模板中的占位符（保持向后兼容：模板 {content}/{query} 不变）
# profile 第 6 套（rolling-mirror-design.md §1）：代码侧旧名 todos/learnings → 模板新名
# stats_facts/records（四块输入语义）；模板另含 prev_mirror/correction_index 占位符。
_ALIASES = {
    "classify": {"content": "content", "open_todos": "open_todos"},
    "classify_single": {"content": "content", "open_todos": "open_todos"},
    "intent": {"query": "query"},
    "chat": {"question": "question", "history": "history", "context": "context"},
    "profile": {
        "todos": "stats_facts", "learnings": "records", "mood_stats": "mood_stats",
        "keywords": "keywords", "active_time": "active_time",
        "total_records": "total_records", "time_range": "time_range",
        "recent_chats": "recent_chats",
        "prev_mirror": "prev_mirror", "correction_index": "correction_index",
        "stats_facts": "stats_facts", "records": "records",
    },
    "inspiration": {"current_input": "current_input", "context": "context"},
    "extract_terms": {"chunks": "chunks", "existing_terms": "existing_terms"},
    "plan_tools": {"tools": "tools", "question": "question"},
}


class PromptLoader:
    """加载并渲染 prompt 模板"""

    def __init__(self):
        self._cache: dict[str, str] = {}

    def load(self, name: str) -> str:
        """按名加载模板文本（带缓存）"""
        if name not in self._cache:
            path = PROMPT_DIR / f"{_FILENAME[name]}.txt"
            self._cache[name] = path.read_text(encoding="utf-8")
        return self._cache[name]

    def render(self, name: str, **kwargs) -> str:
        """加载模板并填充占位符（缺失占位符填空字符串）

        用字面 {key} 替换而非 str.format，因为模板内含 JSON 示例的花括号。
        """
        text = self.load(name)
        allowed = _ALIASES.get(name, {})
        params = {}
        for key, value in kwargs.items():
            target = allowed.get(key, key)
            params[target] = value
        for key in allowed.values():
            params.setdefault(key, "")
        for key, value in params.items():
            text = text.replace("{" + key + "}", str(value))
        return text


# 模块级单例
loader = PromptLoader()
