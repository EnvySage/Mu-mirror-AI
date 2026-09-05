"""第九轮修复单测（不依赖网络 / 不依赖 SDK 行为）

覆盖任务书验证标准中的新增测试：
1. content_type 脏值过滤（chat_service.ExtractIntent 白名单）
2. llm_json.parse_json 统一异常类型（三份收编后全部 ContentInvalidError）
3. SDK 异常翻译映射（openai/anthropic 异常按类型+status_code → errors 子类）
4. marker 排除 markdown 链接（chat_service._find_markers）
5. errors.map_exception 优先级（isinstance 先于关键字）+ safe_details 不泄露原文

运行：.venv/Scripts/python.exe -m pytest tests/test_round9.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # 仓库根，import errors/llm_json/...

from errors import (  # noqa: E402
    AiServiceError,
    ContentInvalidError,
    LlmTimeoutError,
    LlmUnavailableError,
    map_exception,
    safe_details,
    translate_llm_sdk_exception,
)
from llm_json import parse_json  # noqa: E402
from services import chat_service  # noqa: E402

import grpc  # noqa: E402


# ---------------------------------------------------------------------------
# 1. content_type 白名单过滤
# ---------------------------------------------------------------------------
class TestContentTypeWhitelist:
    def test_dirty_value_filtered(self):
        """脏值（如 'SQL 注入'、大写 TODO、未知类型）→ None"""
        assert chat_service.CONTENT_TYPES == {"todo", "thought", "learning", "plan",
                                              "note", "work", "social", "health"}
        for dirty in ("TODO", "Todo", "blog", "sql_injection", "心理健康", ""):
            assert dirty not in chat_service.CONTENT_TYPES, dirty

    def test_valid_values_pass(self):
        for ok in chat_service.CONTENT_TYPES:
            assert ok in chat_service.CONTENT_TYPES


# ---------------------------------------------------------------------------
# 2. parse_json 统一异常类型
# ---------------------------------------------------------------------------
class TestParseJson:
    def test_direct_json(self):
        assert parse_json('{"a": 1}', ctx="意图") == {"a": 1}

    def test_code_fence(self):
        text = '前言\n```json\n{"query_type": "hybrid"}\n```\n后记'
        assert parse_json(text) == {"query_type": "hybrid"}

    def test_brace_substring(self):
        text = '好的，这是结果：{"moods": ["anxious"]} 请查收'
        assert parse_json(text) == {"moods": ["anxious"]}

    def test_no_json_raises_content_invalid(self):
        with pytest.raises(ContentInvalidError):
            parse_json("完全不是 JSON 的回答", ctx="意图")

    def test_broken_fence_raises_content_invalid(self):
        """```json 块命中但内容坏 JSON → 仍是 ContentInvalidError（非裸 ValueError）"""
        with pytest.raises(ContentInvalidError):
            parse_json('```json\n{broken!!}\n```')

    def test_broken_brace_raises_content_invalid(self):
        """括号截取命中但内容坏 JSON → ContentInvalidError（record 版漂移修复点）"""
        with pytest.raises(ContentInvalidError):
            parse_json('{"a": 1, "b": }')

    def test_record_processor_uses_common_impl(self):
        """record_processor 不再有本地副本，用的是公共版（异常类型一致）"""
        from services import record_processor
        assert record_processor.parse_json is parse_json

    def test_chat_profile_use_common_impl(self):
        from services import chat_service, profile_service
        assert chat_service.parse_json is parse_json
        assert profile_service.parse_json is parse_json


# ---------------------------------------------------------------------------
# 3. SDK 异常翻译映射
# ---------------------------------------------------------------------------
def _mk_openai_status_error(status_code: int) -> Exception:
    """构造 openai.APIStatusError（不发起真实网络请求）"""
    from openai import APIStatusError
    import httpx2

    request = httpx2.Request("POST", "https://api.test/v1/chat/completions")
    response = httpx2.Response(status_code, request=request, json={"error": {"message": "x"}})
    return APIStatusError("boom", response=response, body=None)


def _mk_anthropic_status_error(status_code: int) -> Exception:
    from anthropic import APIStatusError
    import httpx

    request = httpx.Request("POST", "https://api.test/v1/messages")
    response = httpx.Response(status_code, request=request, json={"type": "error"})
    return APIStatusError("boom", response=response, body=None)


class TestSdkExceptionTranslation:
    # ---- openai ----
    def test_openai_401(self):
        err = translate_llm_sdk_exception(_mk_openai_status_error(401))
        assert isinstance(err, LlmUnavailableError)
        assert err.code == grpc.StatusCode.UNAVAILABLE

    def test_openai_403(self):
        assert isinstance(translate_llm_sdk_exception(_mk_openai_status_error(403)), LlmUnavailableError)

    def test_openai_404(self):
        assert isinstance(translate_llm_sdk_exception(_mk_openai_status_error(404)), LlmUnavailableError)

    def test_openai_429_rate_limit(self):
        err = translate_llm_sdk_exception(_mk_openai_status_error(429))
        assert isinstance(err, LlmUnavailableError)
        assert "429" in str(err)

    def test_openai_5xx(self):
        for code in (500, 502, 503):
            err = translate_llm_sdk_exception(_mk_openai_status_error(code))
            assert isinstance(err, LlmUnavailableError), code

    # ---- anthropic ----
    def test_anthropic_401(self):
        err = translate_llm_sdk_exception(_mk_anthropic_status_error(401))
        assert isinstance(err, LlmUnavailableError)

    def test_anthropic_429(self):
        err = translate_llm_sdk_exception(_mk_anthropic_status_error(429))
        assert isinstance(err, LlmUnavailableError)

    def test_anthropic_5xx(self):
        err = translate_llm_sdk_exception(_mk_anthropic_status_error(529))  # overloaded
        assert isinstance(err, LlmUnavailableError)

    # ---- timeout / connection（duck-typing 构造）----
    def test_timeout_like_exception(self):
        class APITimeoutError(Exception):
            pass

        assert isinstance(translate_llm_sdk_exception(APITimeoutError("t")), LlmTimeoutError)

    def test_connection_like_exception_is_unavailable_not_timeout(self):
        """APIConnectionError（非超时）→ UNAVAILABLE，不得再被关键字误判为超时"""

        class APIConnectionError(Exception):
            pass

        err = translate_llm_sdk_exception(APIConnectionError("Connection error."))
        assert isinstance(err, LlmUnavailableError)
        assert err.code == grpc.StatusCode.UNAVAILABLE

    def test_openai_real_timeout_class(self):
        from openai import APITimeoutError
        import httpx2

        err = translate_llm_sdk_exception(
            APITimeoutError(httpx2.Request("POST", "https://api.test")))
        assert isinstance(err, LlmTimeoutError)
        assert err.code == grpc.StatusCode.DEADLINE_EXCEEDED

    def test_openai_real_connection_error(self):
        from openai import APIConnectionError
        import httpx2

        err = translate_llm_sdk_exception(
            APIConnectionError(message="conn fail", request=httpx2.Request("POST", "https://x")))
        assert isinstance(err, LlmUnavailableError)

    def test_non_sdk_exception_falls_through(self):
        err = translate_llm_sdk_exception(RuntimeError("something else"))
        assert isinstance(err, AiServiceError)


# ---------------------------------------------------------------------------
# 4. marker 排除 markdown 链接 / 列表编号
# ---------------------------------------------------------------------------
class TestFindMarkers:
    def test_plain_markers(self):
        assert chat_service._find_markers("见[1]，再看[2]") == [1, 2]

    def test_markdown_link_excluded(self):
        """[1](url) / [text](url) 不是引用标记"""
        assert chat_service._find_markers("详情[1](http://example.com/a) 结束") == []
        assert chat_service._find_markers("[2](https://x.y) 和 [1]") == [1]

    def test_list_numbers_excluded(self):
        """'1. xxx' / '1、xxx' 无方括号，天然不匹配；方括号+链接也不匹配"""
        assert chat_service._find_markers("1. 第一条\n2、第二条") == []

    def test_mixed(self):
        answer = "参考[1]与[3](http://x.y)，列表：1. a 2. b，另见[2]"
        assert chat_service._find_markers(answer) == [1, 2]

    def test_non_digit_bracket_excluded(self):
        assert chat_service._find_markers("[注] 和 [abc] 但 [4]") == [4]

    def test_sources_quote_prefers_title(self):
        """quote 对齐 Java 兜底路径：title 优先，空 title 才用 content[:100]"""
        chunk = type("C", (), {"record_id": 7, "title": "三个待办", "content": "x" * 200,
                               "created_at": "2026-09-01"})()
        src = chat_service._extract_sources("[1]", [chunk])[0]
        assert src.quote == "三个待办"

        chunk_no_title = type("C", (), {"record_id": 8, "title": "", "content": "y" * 200,
                                        "created_at": "2026-09-02"})()
        src2 = chat_service._extract_sources("[1]", [chunk_no_title])[0]
        assert src2.quote == "y" * 100


# ---------------------------------------------------------------------------
# 5. map_exception 优先级 + safe_details 不泄露原文
# ---------------------------------------------------------------------------
class TestMapExceptionPriority:
    def test_valueerror_beats_keyword(self):
        """ValueError 族即使文本含 '401'/'timeout' 也归 INVALID_ARGUMENT（isinstance 优先）"""
        err = map_exception(ValueError("bad json contains 401 and timeout"))
        assert isinstance(err, ContentInvalidError)
        assert err.code == grpc.StatusCode.INVALID_ARGUMENT

    def test_aiservice_error_passthrough(self):
        e = LlmTimeoutError("already mapped")
        assert map_exception(e) is e

    def test_timeout_keyword_still_works_as_fallback(self):
        err = map_exception(RuntimeError("read timeout after 20s"))
        assert isinstance(err, LlmTimeoutError)

    def test_unavailable_keyword_fallback(self):
        err = map_exception(RuntimeError("connection refused by upstream"))
        assert isinstance(err, LlmUnavailableError)

    def test_unknown_is_internal(self):
        err = map_exception(RuntimeError("weird thing"))
        assert err.code == grpc.StatusCode.INTERNAL

    def test_apiconnectionerror_no_longer_timeout(self):
        """'apiconnectionerror' 关键字不再归 TIMEOUT（移入 UNAVAILABLE）"""
        err = map_exception(RuntimeError("APIConnectionError: connection error"))
        assert not isinstance(err, LlmTimeoutError)
        assert isinstance(err, LlmUnavailableError)


class TestSafeDetails:
    def test_no_original_message_leak(self):
        mapped = LlmUnavailableError("api key sk-secret-123 leaked host internal-db")
        details = safe_details(mapped)
        assert "sk-secret" not in details
        assert "internal-db" not in details
        assert "LlmUnavailableError" in details

    def test_abort_with_mapped_redacts(self):
        class Ctx:
            def __init__(self):
                self.aborted = None

            def abort(self, code, details):
                self.aborted = (code, details)

        from errors import abort_with_mapped
        ctx = Ctx()
        abort_with_mapped(ctx, RuntimeError("connection refused to 10.0.0.5:5432"))
        code, details = ctx.aborted
        assert code == grpc.StatusCode.UNAVAILABLE
        assert "10.0.0.5" not in details
