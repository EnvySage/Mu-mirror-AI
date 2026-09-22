"""todo-registry 判别期单测（§3.2 AI 侧全量）— open_todos 注入 + TodoRef 解析 + proto 契约

覆盖任务书验证标准：
1. proto 契约（ClassifyRequest.open_todos=5 / ClassifyItem.refers_to_todo=8 / TodoHint / TodoRef，
   与 shared-protocol.md 2026-09-09 登记行一致——B 侧对账依据固化在此；
   撞号核查：ClassifyRequest 现有字段 1-4 无冲突，ClassifyItem 现有字段 1-7 无冲突）
2. 待办清单渲染（todo_render.format_open_todos）：清单在/空清单无孤儿/截断/脏条目跳过
3. TodoRef 解析（_parse_todo_ref）：合法透传 / 非法 status 丢弃 / null 缺失透传为不设值
4. ClassifyItem 组装：refers_to_todo 填充与不填充（wire 层不设值 = 零字节）
5. prompt 接线：两模板 {open_todos} 占位符、无未替换占位符残留、B 未传零回归
6. config todo_hint 段（max_hints/max_excerpt_chars）

运行：.venv/Scripts/python.exe -m pytest tests/test_todo_ref.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import common_pb2 as common  # noqa: E402
from generated import record_processor_pb2 as rp_pb2  # noqa: E402

from prompts_loader import loader  # noqa: E402
from services import record_processor as rps  # noqa: E402
from services.record_processor import _parse_todo_ref  # noqa: E402
from todo_render import format_open_todos  # noqa: E402


def _hint(todo_id=7, title="补作业", excerpt="明天要补作业", created="2026-08-20T10:00:00",
          status="not_started"):
    return rp_pb2.TodoHint(todo_id=todo_id, title=title, source_excerpt=excerpt,
                           created_at=created, current_status=status)


# ---------------------------------------------------------------------------
# 1. proto 契约自检（B 侧对账依据）
# ---------------------------------------------------------------------------
class TestProtoContract:
    def test_classify_request_field_numbers(self):
        """open_todos=5（glossary=4 顺延，无撞号）；recent_context=6（recent-context 轮新增）；
        reference_date=7（相对时间消解，对齐 B 仓）"""
        fields = rp_pb2.ClassifyRequest.DESCRIPTOR.fields_by_name
        assert {n: f.number for n, f in fields.items()} == {
            "content": 1, "llm_config": 2, "single": 3, "glossary": 4, "open_todos": 5,
            "recent_context": 6, "reference_date": 7,
        }

    def test_classify_item_field_numbers(self):
        """refers_to_todo=8（keywords=7 顺延，无撞号）；time_substitutions=9（相对时间消解，对齐 B 仓）"""
        fields = rp_pb2.ClassifyItem.DESCRIPTOR.fields_by_name
        assert {n: f.number for n, f in fields.items()} == {
            "title": 1, "summary": 2, "content": 3, "content_type": 4, "moods": 5,
            "status": 6, "keywords": 7, "refers_to_todo": 8, "time_substitutions": 9,
        }

    def test_todo_hint_fields(self):
        fields = rp_pb2.TodoHint.DESCRIPTOR.fields_by_name
        assert {n: f.number for n, f in fields.items()} == {
            "todo_id": 1, "title": 2, "source_excerpt": 3, "created_at": 4, "current_status": 5,
        }

    def test_todo_ref_fields(self):
        fields = rp_pb2.TodoRef.DESCRIPTOR.fields_by_name
        assert {n: f.number for n, f in fields.items()} == {"todo_id": 1, "suggested_status": 2}

    def test_types(self):
        req_f = rp_pb2.ClassifyRequest.DESCRIPTOR.fields_by_name
        item_f = rp_pb2.ClassifyItem.DESCRIPTOR.fields_by_name
        assert req_f["open_todos"].message_type.full_name == "mirror.TodoHint"
        assert item_f["refers_to_todo"].message_type.full_name == "mirror.TodoRef"
        assert rp_pb2.TodoRef.DESCRIPTOR.fields_by_name["todo_id"].type == 3    # TYPE_INT64
        assert rp_pb2.TodoRef.DESCRIPTOR.fields_by_name["suggested_status"].type == 9  # TYPE_STRING

    def test_wire_first_bytes(self):
        """wire 冒烟：open_todos=5 → 0x2A；refers_to_todo=8 → 0x42；未设值零字节"""
        req = rp_pb2.ClassifyRequest(open_todos=[_hint()])
        assert req.SerializeToString()[0] == 0x2A

        item = rp_pb2.ClassifyItem(refers_to_todo=rp_pb2.TodoRef(todo_id=7, suggested_status="COMPLETED"))
        wire = item.SerializeToString()
        assert wire[0] == 0x42

        back = rp_pb2.ClassifyItem()
        back.ParseFromString(wire)
        assert back.refers_to_todo.todo_id == 7
        assert back.refers_to_todo.suggested_status == "COMPLETED"

        # 无引用不设值：title 之外零字节（B 侧 HasRefersToTodo()=false 判断依据）
        plain = rp_pb2.ClassifyItem(title="x")
        assert plain.SerializeToString() == b"\x0a\x01x"
        assert not plain.HasField("refers_to_todo")

    def test_service_rpcs_unchanged(self):
        """无新增 RPC（Classify + ExtractTerms 两方法）"""
        methods = set(rp_pb2.DESCRIPTOR.services_by_name["RecordProcessor"].methods_by_name)
        assert methods == {"Classify", "ExtractTerms"}

    def test_backward_compat_old_client(self):
        """B 未升级（不传 open_todos）→ wire 上无新字段，旧解析零影响"""
        req = rp_pb2.ClassifyRequest(content="今天学习了 Spring Boot", single=True)
        back = rp_pb2.ClassifyRequest()
        back.ParseFromString(req.SerializeToString())
        assert len(back.open_todos) == 0 and back.content == "今天学习了 Spring Boot"


# ---------------------------------------------------------------------------
# 2. 待办清单渲染
# ---------------------------------------------------------------------------
class TestFormatOpenTodos:
    def test_empty_is_empty_string(self):
        """空清单 → 空串（整块消失不留孤儿——glossary_render 同模式）"""
        assert format_open_todos([]) == ""
        assert format_open_todos(None) == ""

    def test_render_lines(self):
        out = format_open_todos([_hint()])
        assert out.startswith("## 用户未完成的待办清单\n")
        assert "以下是用户此前日记中登记的未完成待办：" in out
        assert "- #7 补作业（登记于 2026-08-20，未开始）：明天要补作业" in out

    def test_render_ends_with_newline(self):
        """末尾带换行：清单与模板规则句之间有分隔"""
        assert format_open_todos([_hint()]).endswith("\n")

    def test_status_cn(self):
        out = format_open_todos([_hint(status="in_progress")])
        assert "进行中" in out

    def test_excerpt_truncation(self):
        long_text = "长" * 300
        out = format_open_todos([_hint(excerpt=long_text)])
        assert ("长" * 300) not in out
        line = next(l for l in out.split("\n") if l.startswith("- #7"))
        assert line.endswith("…"), "清单行尾应有省略号"
        assert ("长" * 100) in line                            # 前 100 字符完整保留
        assert ("长" * 101) not in line                        # 恰好截 100 字符

    def test_excerpt_empty_omitted(self):
        out = format_open_todos([_hint(excerpt="  ")])
        line = next(l for l in out.split("\n") if l.startswith("- #7"))
        assert "补作业（登记于 2026-08-20，未开始）" in line
        assert not line.endswith("："), "无摘要时行尾不应有'：'"

    def test_dirty_entries_skipped(self):
        """todo_id<=0 的脏条目跳过（LLM 拿到也填不出合法 TodoRef）"""
        out = format_open_todos([_hint(todo_id=0), _hint(todo_id=-1), _hint(todo_id=9)])
        assert "#7" not in out
        assert "#9" in out

    def test_all_dirty_is_empty_string(self):
        assert format_open_todos([_hint(todo_id=0)]) == ""

    def test_cap_at_max_hints(self):
        """超过 max_hints 截断保留前 N 条（config todo_hint.max_hints）"""
        from todo_render import _MAX_HINTS
        hints = [_hint(todo_id=i) for i in range(1, _MAX_HINTS + 10)]
        out = format_open_todos(hints)
        assert f"#{_MAX_HINTS}" in out
        assert f"#{_MAX_HINTS + 1}" not in out

    def test_no_title_fallback(self):
        out = format_open_todos([_hint(title="")])
        assert "（无标题）" in out

    def test_no_dirty_date(self):
        out = format_open_todos([_hint(created="")])
        assert "日期未知" in out


# ---------------------------------------------------------------------------
# 3. prompt 模板接线（classify / classify-single 两模板）
# ---------------------------------------------------------------------------
class TestPromptWiring:
    def test_both_templates_have_placeholder(self):
        for name in ("classify", "classify_single"):
            assert "{open_todos}" in loader.load(name), f"{name} 缺 open_todos 占位符"

    def test_template_only_has_placeholder(self):
        """规则与节头只由渲染器产出：模板里不得再残留规则正文 / 节头

        旧版两个模板各写一遍判别规则（改口径要改两处、易漂移），且规则句紧贴
        {open_todos} 会把清单行和规则句粘成一行。现在模板只留占位符。
        """
        for name in ("classify", "classify_single"):
            text = loader.load(name)
            assert "{open_todos}" in text
            # 节头由渲染器产出：空清单时整节消失，不留孤儿节头
            assert "## 用户未完成的待办清单" not in text, f"{name} 残留硬编码节头"
            assert "宁可漏判不可错判" not in text, f"{name} 仍残留绝对化措辞"
            assert "补算法作业" not in text, f"{name} 模板仍残留负向示例"
            assert "进度会因此改变吗" not in text, f"{name} 模板仍残留规则正文"

    def test_rules_present_in_rendered_prompt(self):
        """判别规则随清单渲染进 prompt（步骤也算同一件事 + 可判定判据 + 必填字段）"""
        todos = format_open_todos([_hint()])
        assert "同一件事" in todos
        assert "refers_to_todo" in todos
        assert "NOT_STARTED" in todos and "IN_PROGRESS" in todos and "COMPLETED" in todos
        # 可判定的判据（修漏判核心）：进度是否因此改变
        assert "进度会因此改变吗" in todos
        # 必填语义：无引用时写 todo_id=0
        assert '"todo_id": 0' in todos
        assert "必填" in todos
        # 正向示例：练琴 → 清单"准备学习吉他"属同一条线（真实案例，旧版漏判）
        assert "准备学习吉他" in todos and "练了琴" in todos
        # 负向示例：练琴 与 清单"计划补文献综述" 主题无关、目标不同 → 不填（真实案例，旧版误判）
        assert "计划补文献综述" in todos
        # 双向兜底：多候选取语义最接近的那一个 / 无实质关联宁可不填、不硬扯
        assert "语义最接近" in todos and "宁可不填" in todos
        # 规则确实随渲染进了两个模板的完整 prompt
        for name in ("classify", "classify_single"):
            p = loader.render(name, content="x", glossary="", open_todos=todos)
            assert "进度会因此改变吗" in p, name
            assert "{open_todos}" not in p, name

    def test_output_format_keeps_required_field(self):
        """输出格式示例里保留「无引用」写法：字段必填语义不能只活在规则段里

        规则段随空清单消失，输出格式段是常驻的兜底——它必须给出无引用时的
        {"todo_id": 0, ...} 写法，否则模型在无清单时可能整个字段不输出。
        """
        for name in ("classify", "classify_single"):
            text = loader.load(name)
            assert '"refers_to_todo"' in text, f"{name} 输出格式缺 refers_to_todo"
            assert '"todo_id": 0' in text, f"{name} 输出格式缺无引用写法"
            assert "todo_confidence" in text, f"{name} 输出格式缺 todo_confidence"

    def test_empty_todos_still_keeps_output_format(self):
        """空清单：规则段消失，但输出格式段里的字段写法保留（常驻兜底）"""
        p = loader.render("classify", content="今天吃饭", glossary="",
                          open_todos=format_open_todos([]))
        assert "进度会因此改变吗" not in p          # 规则段随清单消失
        assert '"todo_id": 0' in p                  # 输出格式段常驻
        assert "todo_confidence" in p

    def test_render_with_todos(self):
        p = loader.render("classify_single", content="作业终于补完了", glossary="",
                          open_todos=format_open_todos([_hint()]))
        assert "## 用户未完成的待办清单" in p
        assert "#7 补作业" in p
        assert "{open_todos}" not in p

    def test_list_line_not_glued_to_next_line(self):
        """回归：清单行必须独占一行（旧版渲染产物末尾换行 + 紧贴规则句会粘行）"""
        out = format_open_todos([_hint()])
        for line in out.split("\n"):
            if line.startswith("- #7"):
                assert line.endswith("明天要补作业"), f"清单行被粘连: {line!r}"

    def test_render_empty_no_orphan_list_lines(self):
        """空清单：节头/清单行/规则句全部消失（整节由渲染器产出）"""
        p = loader.render("classify_single", content="今天吃饭", glossary="",
                          open_todos=format_open_todos([]))
        assert "## 用户未完成的待办清单" not in p
        assert "以下是用户此前日记中登记的未完成待办：" not in p
        assert "- #" not in p
        assert "进度会因此改变吗" not in p
        assert "{open_todos}" not in p

    def test_no_unreplaced_placeholders(self):
        import re
        for name in ("classify", "classify_single"):
            p = loader.render(name, content="x", glossary="", open_todos="")
            assert re.findall(r"\{[a-z_]+\}", p) == [], name

    def test_zero_regression_without_new_arg(self):
        """调用方没传 open_todos（旧调用形状）→ 占位符填空串，不炸"""
        p = loader.render("classify", content="正文", glossary="")
        assert "{open_todos}" not in p


# ---------------------------------------------------------------------------
# 4. TodoRef 解析（脏值丢弃）
# ---------------------------------------------------------------------------
class TestParseTodoRef:
    def test_legal_ref_passthrough(self):
        for status in ("NOT_STARTED", "IN_PROGRESS", "COMPLETED"):
            ref = _parse_todo_ref({"refers_to_todo": {"todo_id": 7, "suggested_status": status}})
            assert ref is not None
            assert ref.todo_id == 7 and ref.suggested_status == status

    def test_lowercase_status_normalized(self):
        """LLM 小写输出（completed）宽容大写归一——三态内即透传"""
        ref = _parse_todo_ref({"refers_to_todo": {"todo_id": 7, "suggested_status": "completed"}})
        assert ref is not None and ref.suggested_status == "COMPLETED"

    def test_illegal_status_dropped(self):
        """非法 status（三态外）→ None（脏值丢弃，不透传给 B）"""
        for dirty in ("DONE", "unknown", "FINISHED", "", None, "进行中"):
            assert _parse_todo_ref(
                {"refers_to_todo": {"todo_id": 7, "suggested_status": dirty}}) is None, dirty

    def test_zero_and_negative_id_dropped(self):
        for bad in (0, -1):
            assert _parse_todo_ref(
                {"refers_to_todo": {"todo_id": bad, "suggested_status": "COMPLETED"}}) is None

    def test_non_numeric_id_dropped(self):
        assert _parse_todo_ref(
            {"refers_to_todo": {"todo_id": "abc", "suggested_status": "COMPLETED"}}) is None
        assert _parse_todo_ref(
            {"refers_to_todo": {"todo_id": None, "suggested_status": "COMPLETED"}}) is None

    def test_missing_and_null_passthrough_as_none(self):
        """无引用/null → None（ClassifyItem 不设值，wire 零字节）"""
        assert _parse_todo_ref({}) is None
        assert _parse_todo_ref({"refers_to_todo": None}) is None
        assert _parse_todo_ref({"refers_to_todo": "oops"}) is None
        assert _parse_todo_ref({"refers_to_todo": ["not", "dict"]}) is None

    def test_float_id_truncated(self):
        """浮点 id（LLM 偶发 7.0）int() 截断透传"""
        ref = _parse_todo_ref({"refers_to_todo": {"todo_id": 7.0, "suggested_status": "IN_PROGRESS"}})
        assert ref is not None and ref.todo_id == 7

    def test_required_field_no_reference_form(self):
        """必填契约：模型在无引用时输出 {"todo_id": 0, ...} → 解析为 None（不设值）

        提示词现在要求"无论是否有引用都必须输出 refers_to_todo"，无引用时 todo_id=0。
        该形状必须与"字段缺失"同样安静地退化为不设值，否则会给 B 侧落垃圾建议。
        """
        assert _parse_todo_ref(
            {"refers_to_todo": {"todo_id": 0, "suggested_status": "STATUS_UNKNOWN"},
             "todo_confidence": 0.0}) is None
        assert _parse_todo_ref(
            {"refers_to_todo": {"todo_id": 0, "suggested_status": "COMPLETED"}}) is None

    def test_confidence_field_ignored_not_breaking(self):
        """todo_confidence 是给观测用的附加字段，不参与解析、不影响透传"""
        ref = _parse_todo_ref({"refers_to_todo": {"todo_id": 7, "suggested_status": "IN_PROGRESS"},
                               "todo_confidence": 0.92})
        assert ref is not None and ref.todo_id == 7

    def test_field_number_unchanged_by_confidence(self):
        """todo_confidence 不占 proto 号位（客户端提示词字段，线上协议保持 TodoRef=8 不变）"""
        assert rp_pb2.ClassifyItem.DESCRIPTOR.fields_by_name["refers_to_todo"].number == 8
        assert {f.name for f in rp_pb2.TodoRef.DESCRIPTOR.fields} == {"todo_id", "suggested_status"}


# ---------------------------------------------------------------------------
# 5. ClassifyItem 组装（_build_item 集成）
# ---------------------------------------------------------------------------
class _ScriptedLlm:
    """返回预置文本的桩 LLM"""

    def __init__(self, text):
        self._text = text

    def chat(self, messages, temperature=0.7):
        return self._text


class _CapturedContext:
    def abort(self, code, details):
        raise RuntimeError(f"abort: {code} {details}")


def _classify(llm, **req_kw):
    servicer = rps.RecordProcessorServicer()
    orig = servicer.Classify.__globals__["create_llm"]
    servicer.Classify.__globals__["create_llm"] = lambda **kw: llm
    try:
        return servicer.Classify(rp_pb2.ClassifyRequest(
            content="作业终于补完了，明天继续背单词",
            llm_config=common.LlmConfig(provider="stub"),
            **req_kw,
        ), _CapturedContext())
    finally:
        servicer.Classify.__globals__["create_llm"] = orig


TODO_JSON = ('{"skip": false, "split_content": "作业终于补完了|||明天要背单词", "items": ['
             '{"title": "补作业", "summary": "作业终于补完了", "content_type": "TODO", '
             '"moods": [], "status": "COMPLETED", "keywords": ["作业"], '
             '"refers_to_todo": {"todo_id": 7, "suggested_status": "COMPLETED"}}, '
             '{"title": "背单词", "summary": "明天要背单词", "content_type": "PLAN", '
             '"moods": [], "status": "NOT_STARTED", "keywords": ["单词"]}]}')



class TestBuildItemTodoRef:
    def test_ref_filled_on_item(self):
        """拆分模式：refers_to_todo 填充到对应 item"""
        resp = _classify(_ScriptedLlm(TODO_JSON))
        assert not resp.skip and len(resp.items) == 2
        assert resp.items[0].HasField("refers_to_todo")
        assert resp.items[0].refers_to_todo.todo_id == 7
        assert resp.items[0].refers_to_todo.suggested_status == "COMPLETED"

    def test_no_ref_item_unset(self):
        """无引用的 item：不设值（B 侧 HasRefersToTodo()=false）"""
        resp = _classify(_ScriptedLlm(TODO_JSON))
        assert not resp.items[1].HasField("refers_to_todo")
        assert resp.items[1].refers_to_todo.todo_id == 0  # proto3 message 缺省

    def test_dirty_ref_dropped_at_build(self):
        """脏 ref（非法 status）：构建时丢弃，item 不设值"""
        dirty = TODO_JSON.replace('"todo_id": 7, "suggested_status": "COMPLETED"',
                                  '"todo_id": 7, "suggested_status": "DONE"')
        resp = _classify(_ScriptedLlm(dirty))
        assert not resp.items[0].HasField("refers_to_todo")

    def test_single_mode_ref_filled(self):
        """单段模式同样解析 refers_to_todo"""
        single_json = ('{"skip": false, "title": "补作业", "summary": "作业终于补完了", '
                       '"content_type": "TODO", "moods": [], "status": "COMPLETED", '
                       '"keywords": ["作业"], "refers_to_todo": {"todo_id": 9, "suggested_status": "IN_PROGRESS"}}')
        resp = _classify(_ScriptedLlm(single_json), single=True)
        assert len(resp.items) == 1
        assert resp.items[0].refers_to_todo.todo_id == 9
        assert resp.items[0].refers_to_todo.suggested_status == "IN_PROGRESS"

    def test_open_todos_reaches_prompt(self):
        """请求带 open_todos → 渲染进 prompt（捕获 LLM 收到的消息验证）"""
        captured = {}

        class _Spy(_ScriptedLlm):
            def chat(self, messages, temperature=0.7):
                captured["prompt"] = messages[0]["content"]
                return super().chat(messages, temperature)

        _classify(_Spy(TODO_JSON), open_todos=[_hint()])
        assert "#7 补作业" in captured["prompt"]
        assert "以下是用户此前日记中登记的未完成待办" in captured["prompt"]

    def test_no_open_todos_prompt_unchanged(self):
        """请求不带 open_todos → prompt 无清单行（零回归）"""
        captured = {}

        class _Spy(_ScriptedLlm):
            def chat(self, messages, temperature=0.7):
                captured["prompt"] = messages[0]["content"]
                return super().chat(messages, temperature)

        _classify(_Spy(TODO_JSON))
        assert "以下是用户此前日记中登记的未完成待办" not in captured["prompt"]
        assert "- #" not in captured["prompt"]


# ---------------------------------------------------------------------------
# 6. config todo_hint 段
# ---------------------------------------------------------------------------
class TestConfig:
    def test_todo_hint_section(self):
        from config import CONFIG
        assert CONFIG["todo_hint"]["max_hints"] == 20
        assert CONFIG["todo_hint"]["max_excerpt_chars"] == 100

    def test_render_limits_follow_config(self):
        from todo_render import _MAX_EXCERPT_CHARS, _MAX_HINTS
        assert _MAX_HINTS == 20 and _MAX_EXCERPT_CHARS == 100
