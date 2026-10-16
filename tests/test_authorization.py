"""Fail-closed authorization check for Classify; no database or live model is used."""

import sys
from pathlib import Path

import grpc
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import record_processor_pb2 as pb2  # noqa: E402
from services import record_processor as record_processor  # noqa: E402


class AbortCalled(Exception):
    def __init__(self, code, details):
        self.code = code
        self.details = details


class FakeContext:
    def abort(self, code, details):
        raise AbortCalled(code, details)


def test_classify_rejects_missing_authorization_before_creating_llm(monkeypatch):
    def unexpected_llm(**_kwargs):
        pytest.fail("unauthorized request must be rejected before creating an LLM")

    monkeypatch.setattr(record_processor, "create_llm", unexpected_llm)
    request = pb2.ClassifyRequest(content="这段内容未经授权")

    with pytest.raises(AbortCalled) as error:
        record_processor.RecordProcessorServicer().Classify(request, FakeContext())

    assert error.value.code == grpc.StatusCode.PERMISSION_DENIED
    assert "授权" in error.value.details


def test_classify_rejects_explicit_false_authorization(monkeypatch):
    def unexpected_llm(**_kwargs):
        pytest.fail("false authorization must be rejected before creating an LLM")

    monkeypatch.setattr(record_processor, "create_llm", unexpected_llm)
    request = pb2.ClassifyRequest(content="这段内容未获准", processing_authorized=False)

    with pytest.raises(AbortCalled) as error:
        record_processor.RecordProcessorServicer().Classify(request, FakeContext())

    assert error.value.code == grpc.StatusCode.PERMISSION_DENIED


def test_classify_authorization_field_is_wire_compatible():
    field = pb2.ClassifyRequest.DESCRIPTOR.fields_by_name["processing_authorized"]
    assert field.number == 8
    assert not pb2.ClassifyRequest().processing_authorized
    assert pb2.ClassifyRequest(processing_authorized=True).processing_authorized
