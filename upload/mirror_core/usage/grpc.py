"""Opt-in unary/server-stream usage reports, emitted even on business errors."""
from uuid import UUID

import grpc

from .report import UsageCollector, bind_collector


class UsageServerInterceptor(grpc.ServerInterceptor):
    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None or handler.request_streaming:
            return handler
        invocation = _invocation(handler_call_details.invocation_metadata)
        excluded = handler_call_details.method.startswith("/mirror.EmbeddingService/")

        def unary(request, context):
            collector = UsageCollector(invocation) if invocation and not excluded else None
            with bind_collector(collector):
                try:
                    return handler.unary_unary(request, context)
                finally:
                    _report(context, collector)

        def stream(request, context):
            collector = UsageCollector(invocation) if invocation and not excluded else None
            with bind_collector(collector):
                iterator = None
                try:
                    iterator = handler.unary_stream(request, context)
                    yield from iterator
                finally:
                    try:
                        close = getattr(iterator, "close", None)
                        if callable(close):
                            close()
                    finally:
                        _report(context, collector)

        if handler.unary_unary:
            return grpc.unary_unary_rpc_method_handler(unary, request_deserializer=handler.request_deserializer,
                                                      response_serializer=handler.response_serializer)
        return grpc.unary_stream_rpc_method_handler(stream, request_deserializer=handler.request_deserializer,
                                                   response_serializer=handler.response_serializer)


def _invocation(metadata):
    values = [v for k, v in metadata or () if k == "mirror-usage-invocation-id"]
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    try:
        parsed = str(UUID(values[0]))
        return parsed if parsed == values[0].lower() else None
    except (ValueError, AttributeError):
        return None


def _report(context, collector):
    if collector is None:
        return
    # Collection is diagnostic: a trailer failure must never fail business output.
    try:
        collector.finish(cancelled=not context.is_active())
        existing = tuple(context.trailing_metadata() or ())
        metadata = tuple((k, v) for k, v in existing if k != "mirror-usage-bin")
        context.set_trailing_metadata(metadata + (("mirror-usage-bin", collector.to_bytes()),))
    except Exception:
        pass
