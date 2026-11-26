"""服务级配置加载（config.yml）

仅服务级配置：端口、workers、prompts 路径。
禁止存放用户模型配置（9.3 契约：用户配置随每次 gRPC 请求到达）。
"""

from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yml"
_LOCAL_CONFIG_PATH = Path(__file__).parent / "config.local.yml"
_defaults = {
    "server": {"port": 10003, "workers": 4},
    "agent": {
        "enabled": False,
        "port": 10004,
        "workers": 2,
        "maximum_concurrent_rpcs": 2,
        "total_budget_ms": 480000,
        "loop_budget_ms": 180000,
        "answer_budget_ms": 180000,
        "max_plan_rounds": 4,
        "planning_protocol": "auto",
        "half_life_days": 30,
    },
    "llm": {
        "timeout_seconds": 60,  # 非流式调用总超时（按 max_retries 平摊，单次尝试 30s）
        # Classify 要输出完整 JSON，推理模型在它上面的思考长度波动很大（正常 10~25s，
        # 偶发超过 60s）。单独给更宽的预算，不压缩思考；Java 侧 Classify deadline 180s。
        "classify_timeout_seconds": 120,
        # 流式调用"两块数据之间最多等多久"。原先沿用单次尝试超时（10s），2026-09-21 联调实测
        # mimo 思考中途停顿十几秒即断流，已算好的答案整段作废、用户看到"暂时无法回答"
        "stream_read_timeout_seconds": 60,
        "max_retries": 1,       # SDK 内部重试次数（429/5xx 时），0 禁用
        # anthropic 协议 extended thinking（思考流）预算 token 上限；0 = 关闭。
        # Anthropic 不会默认产出思考块，必须显式开启才有 thinking_delta 事件。
        "thinking_budget_tokens": 2048,
    },
    "extract_terms": {
        "max_chunks": 200,         # 单次抽取语料条数上限（超出按时间窗截断，保留最近）
        "max_chunk_chars": 200,    # 每条语料在 prompt 中的截断长度（防 prompt 膨胀）
        "max_candidates": 10,      # 候选词条上限（prompt 同步声明宁缺毋滥）
    },
    "todo_hint": {
        "max_hints": 20,           # 待办清单注入条数上限（与 B 侧 registry 查询上限 20 同值，双保险）
        "max_excerpt_chars": 100,  # 单条 source_excerpt 在 prompt 中的截断长度（设计稿 §3.2）
    },
    "recent_context": {
        "max_items": 20,           # 清单条数上限（与 B 侧查询上限同值，双保险）
        "max_title_chars": 40,     # 单条 title 渲染截断
        "max_keywords": 5,         # 单条 keywords 渲染上限
    },
    "plan_tools": {"max_tool_chars": 2000},  # Shared Chat tool context limit.
    "mirror": {
        "prev_mirror_max_chars": 30000,      # ① 上一份镜子渲染截断（累计镜子轮）
        "correction_index_max_chars": 8000,  # ③ 校正索引渲染截断
        "record_max_chars": 2000,            # ② 单条记录渲染截断（§3 per_chunk_max_chars 对齐）
    },
    "prompts": {
        "classify": "prompts/classify.txt",
        "classify_single": "prompts/classify-single.txt",
        "agent_intent": "prompts/agent_intent.txt",
        "chat": "prompts/chat.txt",
        "profile": "prompts/profile.txt",
        "inspiration": "prompts/inspiration.txt",
        "extract_terms": "prompts/extract_terms.txt",
    },
}


def load_config() -> dict:
    """加载 config.yml，再叠加可选的 config.local.yml（本机覆盖，不入库）"""
    cfg = _defaults.copy()
    for path in (_CONFIG_PATH, _LOCAL_CONFIG_PATH):
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        for key, value in user_cfg.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key] = {**cfg[key], **value}
            else:
                cfg[key] = value
    return cfg


CONFIG = load_config()
