"""MirrorChat / PlanTools + PlanNextStep — 工具规划（toolcalling-vault-design.md 第 1/5/6 节，
chat-loop-design.md §3/§5.1）

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

PlanNextStep（chat-loop-design.md §3，循环版，新增）：
  B 驱动循环，每步调一次 PlanNextStep(question, history, previous_results, step,
  max_steps, has_retrieval) → 服务端流式 PlanStepChunk：
    ① 先逐块 yield PlanStepChunk(thinking=增量)  —— B 透传 SSE thinking，等待可感知
    ② 最后 yield 一个 PlanStepChunk(final=True, calls=[...], done=bool) 终帧
  **必须用 llm.chat_stream() 而不是 json_task()**：json_task 非流式 + 显式关思考，
  设计稿 §2 查证那 18~20s "黑屏"的根因就是"根本没有可推的东西"（要么没返回要么全返回）。
  chat_stream 已把 Anthropic thinking_delta 与 OpenAI 系 reasoning_content 接成
  ("thinking", text) / ("content", text) 二元组：thinking 立刻外推，content 累积成
  JSON buffer（正文是 JSON，不能往外吐），流结束后统一 parse_json + sanitize_calls。
  旧 PlanTools **保留不删**——配置开关可一键切回（零回归，设计稿 §0.4）。

prompt 模板一分为二（裁决 0.4 / §9 验收 6 的回滚语义要求）：
  - PlanNextStep → prompts/plan_tools.txt（循环版：自评环节 + done 字段 + 循环语境渲染位）
  - PlanTools    → prompts/plan_tools_single.txt（旧版原文快照，逐字不动）
两者共用同一个模板会让 chat-loop-enabled=false 拿到"旧控制流 + 新 prompt"的未验证混合体，
那不是回滚而是第三种模式；回滚路径要退回的是**已知的旧行为**（含旧 prompt 里的已知 bug）。
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
# 循环里"已有材料"逐条渲染的截断长度——与 Chat 渲染工具结果同一口径，复用同一配置项
_MAX_TOOL_CHARS = int(CONFIG["plan_tools"]["max_tool_chars"])

# 循环第一步（previous_results 为空）渲染到 prompt 的中性文案（不留孤儿占位符）
_NO_PREV_RESULTS = "（还没有执行过任何工具，这是本轮第一步）"


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


def format_previous_results(previous_results) -> str:
    """ToolResult 列表 → prompt「已有材料」段落（循环的核心输入）。

    - 空列表 → 中性文案（第一步；prompt 不留孤儿占位符）
    - success=false → 只渲染失败说明，**不把失败 payload 喂给模型**（同 chat_service 口径，
      防止模型把错误输出当事实，也防它原样重试）
    - summary 缺省回退 payload_json，超长截断到 max_tool_chars
    """
    if not previous_results:
        return _NO_PREV_RESULTS
    lines = []
    for i, r in enumerate(previous_results, start=1):
        tool = (r.tool or "").strip() or "unknown_tool"
        if not r.success:
            lines.append(f"{i}. {tool} → 执行失败（这条数据不可用，不要原样重试同一个工具）")
            continue
        body = (r.summary or "").strip() or (r.payload_json or "").strip() or "（无返回数据）"
        if len(body) > _MAX_TOOL_CHARS:
            body = body[:_MAX_TOOL_CHARS] + "…"
        lines.append(f"{i}. {tool} → {body}")
    return "\n".join(lines)


def format_plan_history(history) -> str:
    """ChatMessage 列表（正序）→ prompt 历史段落（多轮指代消解："我焦虑怎么办"接得住上文）。

    窗口由 B 侧控制（设计稿 §4.2 PLAN_HISTORY_ROUNDS=6），本端只渲染，不截断轮数。
    """
    if not history:
        return "（无历史对话）"
    return "\n".join(f"{m.role}: {m.content}" for m in history)


def resolve_done(data: dict, calls: list) -> bool:
    """解析终帧的 done：模型输出的 {"calls":[],"done":true}。

    **calls 为空 → done 恒为 True**：空计划本身就是收尾信号，语义必须与 B 侧两个终止
    条件（done==true / calls 为空）一致，不能出现"空计划 + done=false"让循环白转一圈。
    done 字段容错：布尔直取；字符串 "true"/"yes"/"1" 也认（部分模型会输出字符串）。
    """
    if not calls:
        return True
    raw = data.get("done")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "yes", "1")
    return False


class MirrorChatServicer(_BaseServicer):
    """PlanTools / PlanNextStep servicer——继承 chat_service 的 ExtractIntent/Chat 实现。"""

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
                "plan_tools_single",
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

    def PlanNextStep(self, request, context):
        """循环版「下一步决策」——服务端流式（chat-loop-design.md §3）。

        时序：thinking 增量块 ×N（边想边推，B 透传 SSE thinking）→ 终帧 final=True。
        终帧携带 calls/done；calls 为空或 done=True 由 B 侧判为退出循环。

        异常：流式 RPC，已经 yield 出去的 thinking 块不回滚（B 侧约定"任一步失败就带着
        已有结果退出循环"），这里只按老口径 abort_with_mapped 抛错即可。
        """
        question = request.question
        llm_config = request.llm_config
        registry = format_tool_registry(request.tools)
        registry_names = {(t.name or "").strip() for t in request.tools}
        registry_names.discard("")
        step = request.step or 1
        max_steps = request.max_steps or step

        print(f"[PlanNextStep] step={step}/{max_steps} | "
              f"prev_results={len(request.previous_results)} | "
              f"has_retrieval={request.has_retrieval}")
        print(f"[PlanNextStep] question: {question[:80]} | tools={len(registry_names)} | "
              f"glossary={len(request.glossary)} | history={len(request.history)}")
        print(f"[PlanNextStep] LLM: provider={llm_config.provider}, model={llm_config.model}, "
              f"protocol={llm_config.protocol}")

        try:
            # 注册表为空：无工具可规划 → 直接终帧收尾（不烧 LLM，沿用 PlanTools 口径）
            if not registry_names:
                print("[PlanNextStep] 注册表为空 → 空计划 + done=True（不调 LLM）")
                yield pb2.PlanStepChunk(calls=[], done=True, final=True)
                return

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
                previous_results=format_previous_results(request.previous_results),
                history=format_plan_history(request.history),
                step=step,
                max_steps=max_steps,
                # 小写 true/false 与 prompt 里的字面量对齐（Python 的 True 会让模型读到大写）
                has_retrieval="true" if request.has_retrieval else "false",
            )

            t0 = time.monotonic()
            # chat_stream 而非 json_task：思考重新打开、边想边推（设计稿 §2 黑屏根因）。
            # thinking 立刻外推给 B；content 是 JSON 正文，只能进 buffer 不能外推。
            buffer: list[str] = []
            thinking_blocks = 0
            for kind, text in llm.chat_stream([{"role": "user", "content": prompt}]):
                if kind == "thinking":
                    thinking_blocks += 1
                    yield pb2.PlanStepChunk(thinking=text)
                    continue
                buffer.append(text)

            response_text = "".join(buffer)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            print(f"[PlanNextStep] LLM 耗时 {elapsed_ms}ms | thinking 块 {thinking_blocks} | "
                  f"响应: {response_text[:200]}")

            result = parse_json(response_text, ctx="循环规划")
            calls, dropped = sanitize_calls(result, registry_names)
            if dropped:
                print(f"[PlanNextStep] 剔除违规计划 {dropped} 项"
                      f"（注册表外工具名/args 不可解析/超步数）")
            done = resolve_done(result, calls)
            print(f"[PlanNextStep] step={step}/{max_steps} 终帧：done={done} | "
                  f"计划 {len(calls)} 步: {[(c.tool, c.args_json) for c in calls]}")
            yield pb2.PlanStepChunk(calls=calls, done=done, final=True)

        except Exception as e:
            print(f"[PlanNextStep] 错误: {e}")
            abort_with_mapped(context, e)
