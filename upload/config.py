"""服务级配置加载（config.yml）

仅服务级配置：端口、workers、prompts 路径。
禁止存放用户模型配置（9.3 契约：用户配置随每次 gRPC 请求到达）。
"""

from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yml"
_defaults = {
    "server": {"port": 10003, "workers": 4},
    "llm": {
        "timeout_seconds": 20,  # 单次 LLM 调用超时（Java 客户端 15s 就放弃，本端不应白跑）
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
    "plan_tools": {
        "max_calls": 2,            # 计划步数上限（设计稿"≤2 步"）
        "max_tools": 20,           # 注册表快照渲染条目上限
        "max_arg_chars": 300,      # 单个工具 args_schema 在 prompt 中的截断长度
        "max_tool_chars": 2000,    # Chat 渲染单条工具结果的截断长度
        # 循环规划器的思考预算（仅 anthropic 协议生效；0 = 不传 thinking 参数）。
        # 2026-09-21 联调实测（mimo-v2.5，~36 token/s）：
        #   · 沿用 llm.thinking_budget_tokens=2048 → 光思考 ~57s，第 1 步必撞 B 侧 60s 单步 deadline
        #   · 1024 → 单步 11~60+s 且波动极大，mimo 不严格守预算，显式预算反而诱导它"想满"
        #   · 0    → 单步 9s / 28s，且 mimo 作为原生推理模型照样吐 thinking，思考面板不受影响
        # 换成真 Claude 时注意：0 = 规划器完全不思考（面板在规划阶段为空），届时可设 1024（其下限）
        "thinking_budget_tokens": 0,
    },
    "mirror": {
        "prev_mirror_max_chars": 30000,      # ① 上一份镜子渲染截断（累计镜子轮）
        "correction_index_max_chars": 8000,  # ③ 校正索引渲染截断
        "record_max_chars": 2000,            # ② 单条记录渲染截断（§3 per_chunk_max_chars 对齐）
    },
    "prompts": {
        "classify": "prompts/classify.txt",
        "classify_single": "prompts/classify-single.txt",
        "intent": "prompts/intent.txt",
        "chat": "prompts/chat.txt",
        "profile": "prompts/profile.txt",
        "inspiration": "prompts/inspiration.txt",
        "extract_terms": "prompts/extract_terms.txt",
        "plan_tools": "prompts/plan_tools.txt",
        # 回滚路径专用（chat-loop-design.md 裁决 0.4 / §9 验收 6）：循环版 plan_tools.txt
        # 重写后不能再给旧 PlanTools 用——否则"关掉循环"拿到的是旧控制流 + 新 prompt 的
        # 未验证混合体，那不叫回滚。这份是 HEAD 原文快照，逐字不动（含「情绪安慰」那条 bug，
        # 故意保留：回滚就该退回已知的旧行为）。
        "plan_tools_single": "prompts/plan_tools_single.txt",
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
