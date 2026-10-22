"""RecordProcessor 服务实现

Classify：拆分+分类一次调用；single=true 时进入单段模式（禁止拆分，恰好 1 条）。
"""

import grpc

from generated import common_pb2 as common
from generated import record_processor_pb2 as pb2
from generated import record_processor_pb2_grpc as pb2_grpc

from mirror_core.errors import abort_with_mapped
from mirror_core.glossary_render import format_glossary
from llm.factory import create_llm
from mirror_core.llm_json import parse_json
from mirror_core.prompts_loader import loader
from mirror_core.recent_context_render import format_recent_context
from mirror_core.time_substitution import format_time_rule, parse_reference_date, resolve_substitutions
from mirror_core.todo_render import format_open_todos

# 枚举映射表（proto 枚举名大写）
CONTENT_TYPE_MAP = {
    "TODO": common.ContentType.TODO,
    "THOUGHT": common.ContentType.THOUGHT,
    "LEARNING": common.ContentType.LEARNING,
    "PLAN": common.ContentType.PLAN,
    "NOTE": common.ContentType.NOTE,
    "WORK": common.ContentType.WORK,
    "SOCIAL": common.ContentType.SOCIAL,
    "HEALTH": common.ContentType.HEALTH,
}

MOOD_MAP = {
    "HAPPY": common.MoodType.HAPPY,
    "EXCITED": common.MoodType.EXCITED,
    "SATISFIED": common.MoodType.SATISFIED,
    "GRATEFUL": common.MoodType.GRATEFUL,
    "EXPECTING": common.MoodType.EXPECTING,
    "CALM": common.MoodType.CALM,
    "BORED": common.MoodType.BORED,
    "CONFUSED": common.MoodType.CONFUSED,
    "ANXIOUS": common.MoodType.ANXIOUS,
    "SAD": common.MoodType.SAD,
    "ANGRY": common.MoodType.ANGRY,
    "EXHAUSTED": common.MoodType.EXHAUSTED,
    "STRESSED": common.MoodType.STRESSED,
}

STATUS_MAP = {
    "NOT_STARTED": common.TaskStatus.NOT_STARTED,
    "IN_PROGRESS": common.TaskStatus.IN_PROGRESS,
    "COMPLETED": common.TaskStatus.COMPLETED,
}

# taskStatus 必填：todo/plan 类必须落在三个真实状态里（协作清单 #3）
VALID_STATUS = {common.TaskStatus.NOT_STARTED, common.TaskStatus.IN_PROGRESS, common.TaskStatus.COMPLETED}

# TodoRef.suggested_status 白名单（LLM JSON 里的字符串状态 → 只透传三态，脏值丢弃）
VALID_SUGGESTED_STATUS = {"NOT_STARTED", "IN_PROGRESS", "COMPLETED"}


def _parse_growth_suggestions(result: dict, axes) -> list[common.GrowthSuggestion]:
    """Validate aggregate per-record growth suggestions against the request's exact axes."""
    axes_by_id = {axis.id: axis for axis in axes if axis.id}
    raw = result.get("growth_suggestions", [])
    raw_by_id = {}
    if isinstance(raw, list):
        for candidate in raw:
            if not isinstance(candidate, dict):
                continue
            axis_id = candidate.get("dimension_id")
            points = candidate.get("points")
            if not isinstance(axis_id, str) or axis_id not in axes_by_id:
                continue
            if isinstance(points, bool) or not isinstance(points, int):
                continue
            if not 0 <= points <= 5:
                continue
            raw_by_id.setdefault(axis_id, candidate)
    # Missing/invalid suggestions mean no evidenced contribution, never a guessed score.
    return [common.GrowthSuggestion(
        dimension_id=axis.id,
        points=raw_by_id[axis.id]["points"] if axis.id in raw_by_id else 0,
        reason=str(raw_by_id[axis.id].get("reason", ""))[:500] if axis.id in raw_by_id else "",
    ) for axis in axes if axis.id]


def _parse_todo_ref(item: dict) -> pb2.TodoRef | None:
    """LLM 输出的 refers_to_todo → 合法 TodoRef（脏值丢弃返回 None，不设值）。

    准入条件（任务书 §3）：todo_id > 0 且 suggested_status 在三态内。
    ref 非对象 / todo_id 非数字或 ≤0 / 状态脏值 → 一律 None（宁可漏判不可错判）。
    """
    ref = item.get("refers_to_todo")
    if not isinstance(ref, dict):
        return None
    try:
        todo_id = int(ref.get("todo_id") or 0)
    except (TypeError, ValueError):
        return None
    if todo_id <= 0:
        return None
    status = str(ref.get("suggested_status") or "").strip().upper()
    if status not in VALID_SUGGESTED_STATUS:
        print(f"[Classify] refers_to_todo 脏值丢弃: todo_id={todo_id}, status={status!r}")
        return None
    return pb2.TodoRef(todo_id=todo_id, suggested_status=status)


class RecordProcessorServicer(pb2_grpc.RecordProcessorServicer):

    def Classify(self, request, context):
        if not request.processing_authorized:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "AI 整理未获得用户本次授权")
            return pb2.ClassifyResponse(skip=True, skip_reason="未获得用户授权")
        content = request.content
        llm_config = request.llm_config
        single = request.single

        print(f"[Classify] single={single} 内容: {content[:60]}")
        print(f"[Classify] LLM: provider={llm_config.provider}, model={llm_config.model}, "
              f"api_key={'***' + llm_config.api_key[-4:] if llm_config.api_key else 'EMPTY'}, "
              f"base_url={llm_config.base_url}, protocol={llm_config.protocol}")

        # 内容太短，直接跳过（单段模式同样适用——空片段没有分类价值）
        if len(content.strip()) < 3:
            print("[Classify] 内容太短，跳过")
            return pb2.ClassifyResponse(skip=True, skip_reason="内容太短或无意义", items=[])

        try:
            llm = create_llm(
                provider=llm_config.provider,
                api_key=llm_config.api_key,
                base_url=llm_config.base_url,
                model=llm_config.model,
                protocol=llm_config.protocol,
            )

            glossary_text = format_glossary(request.glossary)
            if request.glossary:
                print(f"[Classify] glossary 注入 {len(request.glossary)} 条")

            # 待办清单注入（todo-registry 判别期 §3.2）：B 查未完成 registry 传入，
            # 空/未传 → 空串，prompt 与旧版一致（glossary 同模式零回归）
            todos_text = format_open_todos(request.open_todos)
            if request.open_todos:
                print(f"[Classify] open_todos 注入 {len(request.open_todos)} 条")

            # 近期语境注入（recent-context）：B 查近 7 天记录摘要传入，
            # 空/未传 → 空串，prompt 与旧版一致（glossary/todos 同模式零回归）
            recent_text = format_recent_context(request.recent_context)
            if request.recent_context:
                print(f"[Classify] recent_context 注入 {len(request.recent_context)} 条")

            # 相对时间消解（time_substitution.py）：B 传参照日期 = 记录创建日；
            # 未传/非法 → 不渲染本节、不产出替换（prompt 与旧版一致，零回归）
            ref_date = parse_reference_date(request.reference_date)
            if ref_date:
                print(f"[Classify] reference_date={ref_date.isoformat()}（相对时间消解开启）")

            growth_context_text = self._format_growth_context(request.growth_context.axes)
            if single:
                return self._classify_single(llm, content, glossary_text, todos_text, recent_text,
                                             format_time_rule(ref_date, single=True), ref_date,
                                             request.growth_context.axes, growth_context_text)
            return self._classify_split(llm, content, glossary_text, todos_text, recent_text,
                                        format_time_rule(ref_date), ref_date,
                                        request.growth_context.axes, growth_context_text)

        except Exception as e:
            print(f"[Classify] 错误: {e}")
            abort_with_mapped(context, e)

    # ------------------------------------------------------------------
    # 单段模式：用户确认过边界的完整片段，禁止拆分，恰好 1 条 ClassifyItem
    # ------------------------------------------------------------------
    def _classify_single(self, llm, content: str, glossary_text: str = "", todos_text: str = "",
                         recent_text: str = "", time_rule: str = "", ref_date=None,
                         growth_axes=(), growth_context_text=""):
        prompt = loader.render("classify_single", content=content, glossary=glossary_text,
                               open_todos=todos_text, recent_context=recent_text, time_rule=time_rule,
                               growth_context=growth_context_text)
        response_text = llm.chat([{"role": "user", "content": prompt}])
        print(f"[Classify/single] LLM 响应: {response_text[:200]}")

        result = parse_json(response_text, ctx="分类")

        if result.get("skip", False):
            return pb2.ClassifyResponse(
                skip=True,
                skip_reason=result.get("skip_reason", ""),
                items=[],
            )

        item_data = result.get("items", [result]) if isinstance(result.get("items", None), list) else [result]
        if not item_data:
            item_data = [result]

        # 恰好 1 条：取第一条，原文就是请求的完整片段。
        # 替换表按请求原片段校验：B 侧单段路径（ChunkServiceImpl）是对请求里的 segmentText 执行替换
        item = self._build_item(item_data[0], fallback_content=content, ref_date=ref_date,
                                subs_text=content, outer_subs=result.get("time_substitutions"))
        print(f"[Classify/single] 返回 1 条: {item.title}")
        return pb2.ClassifyResponse(skip=False, skip_reason="", items=[item],
                                    growth_suggestions=_parse_growth_suggestions(result, growth_axes))

    # ------------------------------------------------------------------
    # 拆分模式：一次 LLM 调用完成拆分+分类
    # ------------------------------------------------------------------
    def _classify_split(self, llm, content: str, glossary_text: str = "", todos_text: str = "",
                        recent_text: str = "", time_rule: str = "", ref_date=None,
                        growth_axes=(), growth_context_text=""):
        prompt = loader.render("classify", content=content, glossary=glossary_text,
                               open_todos=todos_text, recent_context=recent_text, time_rule=time_rule,
                               growth_context=growth_context_text)
        response_text = llm.chat([{"role": "user", "content": prompt}])
        print(f"[Classify] LLM 响应: {response_text[:200]}")

        result = parse_json(response_text, ctx="分类")

        if result.get("skip", False):
            print(f"[Classify] 跳过: {result.get('skip_reason', '')}")
            return pb2.ClassifyResponse(
                skip=True,
                skip_reason=result.get("skip_reason", ""),
                items=[],
            )

        items_data = result.get("items", [])
        split_content = result.get("split_content", "")

        # 兜底：没有 items 视为无法解析
        if not items_data:
            print("[Classify] LLM 返回空 items，跳过")
            return pb2.ClassifyResponse(skip=True, skip_reason="无法解析内容", items=[])

        # 用 ||| 分割原文，对应到每条 item
        content_parts = [p.strip() for p in split_content.split("|||") if p.strip()]
        print(f"[Classify] split_content: {repr(split_content)}")
        print(f"[Classify] content_parts: {content_parts}")

        # 外层字段容忍（2026-09-22 真机实测）：模型只拆出 1 条时，常把 time_substitutions
        # 提到与 items 平级的顶层。多条时归属不明，不能猜——那种情况只认 item 内的。
        outer_subs = result.get("time_substitutions") if len(items_data) == 1 else None
        if outer_subs:
            print(f"[Classify] time_substitutions 出现在 items 外层（单条场景），已采纳")

        items = []
        for i, item in enumerate(items_data):
            original_content = content_parts[i] if i < len(content_parts) else item.get("summary", "")
            items.append(self._build_item(item, fallback_content=original_content, ref_date=ref_date,
                                          outer_subs=outer_subs))
        print(f"[Classify] 返回 {len(items)} 条记录")
        return pb2.ClassifyResponse(skip=False, skip_reason="", items=items,
                                    growth_suggestions=_parse_growth_suggestions(result, growth_axes))

    @staticmethod
    def _format_growth_context(axes) -> str:
        if not axes:
            return ""
        rendered = "\n".join(
            f"- id={axis.id}; name={axis.name}; description={axis.description}; "
            f"definition_version={axis.definition_version}"
            for axis in axes
        )
        return ("## 成长贡献评估（仅评本条记录）\n"
                "根据下方 Java 传入的成长维度原样评估这条记录体现的具体活动贡献。"
                "评价本条活动证据，不评价用户人格；every axis can be zero。"
                "仅有明确证据时给 0..5 的整数分，无证据为 0，不得编造贡献。\n"
                f"轴定义（ID 必须逐字使用）：\n{rendered}\n")

    # ------------------------------------------------------------------
    def _build_item(self, item: dict, fallback_content: str, ref_date=None,
                    subs_text: str | None = None, outer_subs=None) -> pb2.ClassifyItem:
        """dict → ClassifyItem，含枚举兜底 + taskStatus 必填保证 + 时间词替换表

        subs_text：替换表的校验文本（original 必须逐字出现在其中）。须与 B 侧实际执行替换的
        文本一致——拆分模式是 ClassifyItem.content（缺省即用它），单段模式是请求原片段。
        outer_subs：模型把 time_substitutions 放到 items 外层时的顶层值（仅单条场景传入）；
        item 内已有时以其为准。
        """
        content_type = CONTENT_TYPE_MAP.get(
            str(item.get("content_type", "")).upper(), common.ContentType.CONTENT_UNKNOWN
        )
        moods = [MOOD_MAP[m.upper()] for m in item.get("moods", []) if str(m).upper() in MOOD_MAP]
        status = STATUS_MAP.get(str(item.get("status", "")).upper(), common.TaskStatus.STATUS_UNKNOWN)

        # taskStatus 必填保证：TODO/PLAN 必须是三个真实状态之一；
        # LLM 漏填/乱填时兜底为 NOT_STARTED，绝不让 Java 端拿到 UNKNOWN 的待办
        if content_type in (common.ContentType.TODO, common.ContentType.PLAN) and status not in VALID_STATUS:
            print(f"[Classify] taskStatus 兜底: {item.get('status')!r} -> NOT_STARTED")
            status = common.TaskStatus.NOT_STARTED

        item_content = item.get("content") or fallback_content
        raw_subs = item.get("time_substitutions")
        if not raw_subs:
            raw_subs = outer_subs
        subs = resolve_substitutions(raw_subs,
                                     subs_text if subs_text is not None else item_content, ref_date)
        if subs:
            print(f"[Classify] 时间词替换 {len(subs)} 处: {subs}")

        raw_tags = item.get("user_tags", [])
        user_tags = ([tag.strip() for tag in raw_tags if isinstance(tag, str) and tag.strip()][:20]
                     if isinstance(raw_tags, list) else [])
        return pb2.ClassifyItem(
            title=item.get("title", ""),
            summary=item.get("summary", ""),
            content=item_content,
            content_type=content_type,
            moods=moods,
            status=status,
            keywords=item.get("keywords", []),
            user_tags=user_tags,
            **({"refers_to_todo": ref} if (ref := _parse_todo_ref(item)) else {}),
            time_substitutions=[pb2.TimeSubstitution(original=o, resolved=r) for o, r in subs],
        )
