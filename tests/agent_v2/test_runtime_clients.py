from types import SimpleNamespace

from embedding.api_embedder import ApiEmbedder
from llm.anthropic_llm import AnthropicLlm
from llm.openai_llm import OpenAiLlm, _STREAM_TIMEOUT


class FakeClient:
    def __init__(self, response=None):
        self.response = response
        self.options = None
        self.kwargs = None

    def with_options(self, **kwargs):
        self.options = kwargs
        return self

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self.create))

    @property
    def messages(self):
        return SimpleNamespace(create=self.create, stream=self.stream)

    @property
    def embeddings(self):
        return SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_openai_chat_forwards_timeout_and_disables_sdk_retries_for_v2():
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    client = FakeClient(response)
    llm = OpenAiLlm.__new__(OpenAiLlm)
    llm.model = "m"
    llm.client = client

    assert llm.chat([], timeout=1.25, max_retries=0) == "ok"
    assert client.options == {"timeout": 1.25, "max_retries": 0}
    assert client.kwargs["model"] == "m"


def test_anthropic_chat_forwards_timeout_and_disables_sdk_retries():
    response = SimpleNamespace(content=[SimpleNamespace(text="ok")])
    client = FakeClient(response)
    llm = AnthropicLlm.__new__(AnthropicLlm)
    llm.model = "m"
    llm.client = client

    assert llm.chat([{"role": "user", "content": "hi"}], timeout=2, max_retries=0) == "ok"
    assert client.options == {"timeout": 2, "max_retries": 0}
    assert client.kwargs["model"] == "m"


def test_openai_stream_closes_when_consumer_cancels():
    class Stream:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="first"))])
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="second"))])

        def close(self):
            self.closed = True

    stream = Stream()
    client = FakeClient(stream)
    llm = OpenAiLlm.__new__(OpenAiLlm)
    llm.model = "m"
    llm.client = client
    events = llm.chat_stream([], timeout=1, max_retries=0)

    assert next(events) == ("content", "first")
    events.close()
    assert stream.closed is True
    assert client.options == {"max_retries": 0}
    assert client.kwargs["timeout"] == 1


def test_anthropic_stream_context_closes_on_generator_cancellation(monkeypatch):
    class StreamContext:
        def __init__(self):
            self.closed = False

        def __enter__(self):
            return iter([SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="first"))])

        def __exit__(self, *_args):
            self.closed = True

    context = StreamContext()
    client = FakeClient(context)
    llm = AnthropicLlm.__new__(AnthropicLlm)
    llm.model = "m"
    llm.client = client
    monkeypatch.setattr("llm.anthropic_llm._THINKING_BUDGET", 0)
    events = llm.chat_stream([{"role": "user", "content": "hi"}], timeout=1, max_retries=0)

    assert next(events) == ("content", "first")
    events.close()
    assert context.closed is True
    assert client.options == {"max_retries": 0}
    assert client.kwargs["timeout"] == 1


def test_api_embedder_forwards_deadline_and_legacy_no_override(monkeypatch):
    client = FakeClient(SimpleNamespace(data=[SimpleNamespace(embedding=[1.0] * 1024)]))
    embedder = ApiEmbedder.__new__(ApiEmbedder)
    embedder.model_name = "embed-model"
    embedder.client = client

    assert len(embedder.embed("query", timeout=0.75, max_retries=0)) == 1024
    assert client.options == {"timeout": 0.75, "max_retries": 0}
    assert client.kwargs == {"model": "embed-model", "input": "query"}


def test_openai_stream_uses_timeout_type_from_the_locked_openai_sdk():
    from openai import Timeout as OpenAITimeout

    assert isinstance(_STREAM_TIMEOUT, OpenAITimeout)
