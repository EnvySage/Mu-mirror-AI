"""A valid classification must get one uninterrupted model request budget."""

import json

import httpx
import pytest
from openai import OpenAI

from generated import common_pb2 as common
from generated import record_processor_pb2 as pb2
from llm import openai_llm
from services import record_processor


@pytest.mark.parametrize("single", [False, True])
def test_classification_accepts_provider_that_needs_45_seconds_within_60_second_budget(monkeypatch, single):
    payload = {
        "skip": False,
        "split_content": "中午吃饭|||晚上去了青杉面馆",
        "items": [
            {"title": "午饭", "content_type": "NOTE", "embedding_enabled": False},
            {"title": "餐馆体验", "content_type": "NOTE", "embedding_enabled": True},
        ],
    }

    def provider(request):
        # Model a response that needs 45 seconds without sleeping in the test.
        # httpx receives the actual timeout selected by the production SDK path.
        if request.extensions["timeout"]["read"] < 45:
            raise httpx.ReadTimeout("provider needs 45 seconds", request=request)
        return httpx.Response(200, json={
            "id": "classification", "object": "chat.completion", "created": 0,
            "model": "synthetic", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": json.dumps(payload)},
                "finish_reason": "stop"}],
        })

    monkeypatch.setitem(record_processor.CONFIG["llm"], "timeout_seconds", 60)
    monkeypatch.setattr(openai_llm, "_LLM_ATTEMPT_TIMEOUT", 30)
    monkeypatch.setattr(openai_llm, "_LLM_MAX_RETRIES", 1)
    with httpx.Client(transport=httpx.MockTransport(provider)) as transport:
        monkeypatch.setattr(openai_llm, "OpenAI", lambda **kwargs: OpenAI(http_client=transport, **kwargs))

        class Context:
            def abort(self, code, details):
                pytest.fail(f"valid classification was aborted: {code}: {details}")

        response = record_processor.RecordProcessorServicer().Classify(pb2.ClassifyRequest(
            content="中午吃饭。晚上去了青杉面馆。", single=single, processing_authorized=True,
            llm_config=common.LlmConfig(provider="openai", protocol=common.AiProtocol.OPENAI,
                api_key="unit-test", base_url="http://llm.test/v1", model="synthetic"),
        ), Context())

    assert not response.skip
    assert len(response.items) == (1 if single else 2)
    assert response.items[0].embedding_enabled is False
    if not single:
        assert response.items[1].embedding_enabled is True
