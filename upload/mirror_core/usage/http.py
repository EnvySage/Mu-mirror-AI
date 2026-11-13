"""SDK default clients with pass-through per-network-attempt observation.

Using the SDK defaults keeps pool, redirects, environment proxies and timeouts.
Each SDK retry passes through _send_single_request; no retry is added here.
"""
from openai import DefaultHttpxClient as OpenAIDefaultClient
from anthropic import DefaultHttpxClient as AnthropicDefaultClient

from .project import ResponseProjection
from .report import current_collector


class ObservedClient:
    def __init__(self, *, model, provider="", **kwargs):
        self.usage_model = model
        self.usage_provider = provider
        super().__init__(**kwargs)

    def __del__(self):
        # Supplying a custom HTTP client bypasses the SDK's owned-client wrapper.
        # Retain its cleanup contract for legacy callers without explicit close.
        try:
            if not self.is_closed:
                self.close()
        except Exception:
            pass

    def _send_single_request(self, request):
        collector = current_collector()
        observation = collector.begin(self.usage_protocol, self.usage_provider, self.usage_model) if collector else None
        if observation is None:
            return super()._send_single_request(request)
        try:
            response = super()._send_single_request(request)
        except BaseException:
            observation.finish("error")
            raise
        observation.http_status = response.status_code
        observation.streaming = "text/event-stream" in response.headers.get("content-type", "")
        parser = ResponseProjection(observation, observation.streaming)
        status = "success" if response.is_success else "error"
        if response.is_stream_consumed:
            parser.feed(response.content)
            parser.eof()
            observation.finish(status)
            return response
        original_iter, original_close = response.iter_bytes, response.close

        def iter_bytes(chunk_size=None):
            try:
                for data in original_iter(chunk_size):
                    parser.feed(data)
                    yield data
                parser.eof()
                observation.finish(status, force=True)
            except GeneratorExit:
                observation.finish(status if observation.terminal else "cancelled")
                raise
            except BaseException:
                observation.finish("error", force=True)
                raise

        def close():
            try:
                original_close()
            finally:
                observation.finish(status if observation.terminal else "cancelled")

        response.iter_bytes = iter_bytes
        response.close = close
        return response


class OpenAIUsageClient(ObservedClient, OpenAIDefaultClient):
    usage_protocol = "openai"


class AnthropicUsageClient(ObservedClient, AnthropicDefaultClient):
    usage_protocol = "anthropic"
