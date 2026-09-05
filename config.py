"""服务级配置加载（config.yml）

仅服务级配置：端口、workers、prompts 路径。
禁止存放用户模型配置（9.3 契约：用户配置随每次 gRPC 请求到达）。
"""

from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yml"
_defaults = {
    "server": {"port": 50051, "workers": 4},
    "llm": {
        "timeout_seconds": 20,  # 单次 LLM 调用超时（Java 客户端 15s 就放弃，本端不应白跑）
        "max_retries": 1,       # SDK 内部重试次数（429/5xx 时），0 禁用
    },
    "extract_terms": {
        "max_chunks": 200,         # 单次抽取语料条数上限（超出按时间窗截断，保留最近）
        "max_chunk_chars": 200,    # 每条语料在 prompt 中的截断长度（防 prompt 膨胀）
        "max_candidates": 10,      # 候选词条上限（prompt 同步声明宁缺毋滥）
    },
    "prompts": {
        "classify": "prompts/classify.txt",
        "classify_single": "prompts/classify-single.txt",
        "intent": "prompts/intent.txt",
        "chat": "prompts/chat.txt",
        "profile": "prompts/profile.txt",
        "inspiration": "prompts/inspiration.txt",
        "extract_terms": "prompts/extract_terms.txt",
    },
}


def load_config() -> dict:
    """加载 config.yml，缺失时返回默认值"""
    cfg = _defaults.copy()
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        for key, value in user_cfg.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key] = {**cfg[key], **value}
            else:
                cfg[key] = value
    return cfg


CONFIG = load_config()
