"""MirrorChat / PlanTools — 工具规划（toolcalling-vault-design.md 第 1/5/6 节）

服务归属：并入 MirrorChat 服务（services/chat_service.py 的 MirrorChatServicer 子类），
不建新 gRPC 服务——设计稿第 6 节契约即"并入 MirrorChat"（rpc PlanTools 注释），B 侧复用
同一 channel 无需新连接；与词典轮 ExtractTerms 并入 RecordProcessor 同一先例。

流程（设计稿第 1 节）：
  B 调 PlanTools(question, glossary, tools, llm_config)
  → LLM 按 JSON 约定输出 {"calls":[{"tool":"...","args":{...}}]}（≤2 步）
  → Python 校验后返回 PlannedCall；B 执行工具并落 tool_calls 审计
  → 结果塞 ChatRequest.tool_results → 正常 Chat 流式（tool_results 渲染见 chat_service）

无状态铁律：工具注册表由 Java 传入（本文件不硬编码任何工具清单），Python 只渲染 prompt
+ 校验 LLM 输出；不缓存用户数据，失败走 errors.py 统一映射（abort_with_mapped）。

PlannedCall 校验（任务书 #2，违规剔除而非报错——防幻觉 + 失败隔离）：
  - tool 不在传入注册表内 → 静默剔除 + 日志
  - args_json 不可解析 / 非对象 → 剔除该项
  - calls 超 max_calls（默认 2）→ 截断保留前 N 步
"""

import json
import time

from generated import mirror_chat_pb2 as pb2
from generated import mirror_chat_pb2_grpc as pb2_grpc

from services.chat_service import MirrorChatServicer as _BaseServicer
from config import CONFIG
from errors import abort_with_mapped
from glossary_render import format_glossary
from llm.factory import create_llm
from llm_json import parse_json
from prompts_loader import loader

# 上限（config plan_tools 段，各上限配置化）
_MAX_CALLS = int(CONFIG["plan_tools"]["max_calls"])
_MAX_TOOLS = int(CONFIG["plan_tools"]["max_tools"])
_MAX_ARG_CHARS = int(CONFIG["plan_tools"]["max_arg_chars"])


def format_tool_registry(tools) -> str:
    """ToolSpec 注册表快照 → prompt 段落（逐条 name/description/args_schema）。

    Java 未传注册表 → 返回空串（prompt 不留孤儿节，后续逻辑自然返回空计划）。
    超过 max_tools 截断（prompt 膨胀防线）。
    """
    if not tools:
        return ""
    lines = []
    for i, t in enumerate(list(tools)[:_MAX_TOOLS], start=1):
        name = (t.name or "").strip()
        if not name:
            continue
        desc = (t.description or "").strip()
        schema = (t.args_schema or "").strip()
        lines.append(f"{i}. {name}：{desc}" if desc else f"{i}. {name}")
        if schema:
            lines.append(f"   参数：{schema[:_MAX_ARG_CHARS]}")
    return "\n".join(lines)


def sanitize_calls(data: dict, registry_names: set[str]) -> list[pb2.PlannedCall]:
    """LLM JSON → 合法 PlannedCall 列表（校验 + 剔除，不报错）。

    - {"calls": [...]} 之外的结构（含 calls 非列表）→ 空计划
    - tool 必须在传入注册表内（防幻觉调用）：注册表外/空名静默剔除 + 计数
    - args 不可解析 / 非对象 → 剔除该项
    - 超过 max_calls 截断保留前 N 步（设计稿"≤2 步"）
    返回 (calls, dropped_count)。
    """
    raw = data.get("calls")
    if not isinstance(raw, list):
        return [], 0
    out: list[pb2.PlannedCall] = []
    dropped = 0
    for item in raw:
        if len(out) >= _MAX_CALLS:
            dropped += 1
            continue
        if not isinstance(item, dict):
            dropped += 1
            continue
        tool = str(item.get("tool", "")).strip()
        if not tool or tool not in registry_names:
            dropped += 1  # 幻觉工具名：注册表外 → 静默剔除 + 日志（servicer 层打印）
            continue
        args = item.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                dropped += 1
                continue
        if not isinstance(args, dict):
            args = {}  # 缺 args/非对象：工具执行侧自兜底默认值，不算违规
        try:
            args_json = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            dropped += 1
            continue
        out.append(pb2.PlannedCall(tool=tool, args_json=args_json))
    return out, dropped


class MirrorChatServicer(_BaseServicer):
    """PlanTools servicer——继承 chat_service 的 ExtractIntent/Chat 实现，新增 PlanTools。"""

    def PlanTools(self, request, context):
        question = request.question
        llm_config = request.llm_config
        registry = format_tool_registry(request.tools)
        registry_names = {(t.name or "").strip() for t in request.tools}
        registry_names.discard("")

        print(f"[PlanTools] question: {question[:80]} | tools={len(registry_names)} | "
              f"glossary={len(request.glossary)}")
        print(f"[PlanTools] LLM: provider={llm_config.provider}, model={llm_config.model}, "
              f"protocol={llm_config.protocol}")

        try:
            # 注册表为空：无工具可规划，直接返回空计划（不烧 LLM——宁可少规划）
            if not registry_names:
                print("[PlanTools] 注册表为空 → 空计划")
                return pb2.PlanToolsReply(calls=[])

            llm = create_llm(
                provider=llm_config.provider,
                api_key=llm_config.api_key,
                base_url=llm_config.base_url,
                model=llm_config.model,
                protocol=llm_config.protocol,
            )

            prompt = loader.render(
                "plan_tools",
                tools=registry if registry else "（本次无可用工具）",
                question=question,
                glossary=format_glossary(request.glossary),
            )

            t0 = time.monotonic()
            # json_task：关思考（工具规划是 JSON 决策，不需要思维链）。实测 18s 级别
            # 的耗时主要烧在思考 token 上，且它在"用户看到第一个字之前"串行执行。
            response_text = llm.json_task([{"role": "user", "content": prompt}])
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            print(f"[PlanTools] LLM 耗时 {elapsed_ms}ms | 响应: {response_text[:200]}")

            result = parse_json(response_text, ctx="工具规划")
            calls, dropped = sanitize_calls(result, registry_names)
            if dropped:
                print(f"[PlanTools] 剔除违规计划 {dropped} 项"
                      f"（注册表外工具名/args 不可解析/超步数）")
            print(f"[PlanTools] 计划 {len(calls)} 步: "
                  f"{[(c.tool, c.args_json) for c in calls]}")
            return pb2.PlanToolsReply(calls=calls)

        except Exception as e:
            print(f"[PlanTools] 错误: {e}")
            abort_with_mapped(context, e)
