"""RecordProcessor 服务实现

Classify：拆分+分类一次调用；single=true 时进入单段模式（禁止拆分，恰好 1 条）。
"""

import grpc

from generated import common_pb2 as common
from generated import record_processor_pb2 as pb2
from generated import record_processor_pb2_grpc as pb2_grpc

from errors import abort_with_mapped
from glossary_render import format_glossary
from llm.factory import create_llm
from llm_json import parse_json
from prompts_loader import loader
from recent_context_render import format_recent_context
from time_substitution import format_time_rule, parse_reference_date, resolve_substitutions
from todo_render import format_open_todos

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
            time_rule = format_time_rule(ref_date)
            if ref_date:
                print(f"[Classify] reference_date={ref_date.isoformat()}（相对时间消解开启）")

            if single:
                return self._classify_single(llm, content, glossary_text, todos_text, recent_text,
                                             time_rule, ref_date)
            return self._classify_split(llm, content, glossary_text, todos_text, recent_text,
                                        time_rule, ref_date)

        except Exception as e:
            print(f"[Classify] 错误: {e}")
            abort_with_mapped(context, e)

    # ------------------------------------------------------------------
    # 单段模式：用户确认过边界的完整片段，禁止拆分，恰好 1 条 ClassifyItem
    # ------------------------------------------------------------------
    def _classify_single(self, llm, content: str, glossary_text: str = "", todos_text: str = "",
                         recent_text: str = "", time_rule: str = "", ref_date=None):
        prompt = loader.render("classify_single", content=content, glossary=glossary_text,
                               open_todos=todos_text, recent_context=recent_text, time_rule=time_rule)
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
        item = self._build_item(item_data[0], fallback_content=content, ref_date=ref_date, subs_text=content)
        print(f"[Classify/single] 返回 1 条: {item.title}")
        return pb2.ClassifyResponse(skip=False, skip_reason="", items=[item])

    # ------------------------------------------------------------------
    # 拆分模式：一次 LLM 调用完成拆分+分类
    # ------------------------------------------------------------------
    def _classify_split(self, llm, content: str, glossary_text: str = "", todos_text: str = "",
                        recent_text: str = "", time_rule: str = "", ref_date=None):
        prompt = loader.render("classify", content=content, glossary=glossary_text,
                               open_todos=todos_text, recent_context=recent_text, time_rule=time_rule)
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

        items = []
        for i, item in enumerate(items_data):
            original_content = content_parts[i] if i < len(content_parts) else item.get("summary", "")
            items.append(self._build_item(item, fallback_content=original_content, ref_date=ref_date))
        print(f"[Classify] 返回 {len(items)} 条记录")
        return pb2.ClassifyResponse(skip=False, skip_reason="", items=items)

    # ------------------------------------------------------------------
    def _build_item(self, item: dict, fallback_content: str, ref_date=None,
                    subs_text: str | None = None) -> pb2.ClassifyItem:
        """dict → ClassifyItem，含枚举兜底 + taskStatus 必填保证 + 时间词替换表

        subs_text：替换表的校验文本（original 必须逐字出现在其中）。须与 B 侧实际执行替换的
        文本一致——拆分模式是 ClassifyItem.content（缺省即用它），单段模式是请求原片段。
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
        subs = resolve_substitutions(item.get("time_substitutions"),
                                     subs_text if subs_text is not None else item_content, ref_date)
        if subs:
            print(f"[Classify] 时间词替换 {len(subs)} 处: {subs}")

        return pb2.ClassifyItem(
            title=item.get("title", ""),
            summary=item.get("summary", ""),
            content=item_content,
            content_type=content_type,
            moods=moods,
            status=status,
            keywords=item.get("keywords", []),
            **({"refers_to_todo": ref} if (ref := _parse_todo_ref(item)) else {}),
            time_substitutions=[pb2.TimeSubstitution(original=o, resolved=r) for o, r in subs],
        )
