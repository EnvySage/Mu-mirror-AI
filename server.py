"""Mirror AI gRPC 服务入口"""

import time
from concurrent import futures
import os

import grpc

from config import CONFIG
from generated import record_processor_pb2_grpc as rp_grpc
from generated import embedding_pb2_grpc as emb_grpc
from generated import mirror_chat_pb2_grpc as chat_grpc
from generated import mirror_profile_pb2_grpc as profile_grpc

from services.lexicon_service import RecordProcessorServicer
from services.embedding_service import EmbeddingServiceServicer
from services.plan_service import MirrorChatServicer
from services.profile_service import MirrorProfileServicer
from services.agent_service import AgentRunnerServicer
from generated import agent_pb2_grpc as agent_grpc
from generated import tarot_pb2_grpc as tarot_grpc
from services.tarot_service import TarotReadingServicer


def build_legacy_server(*, port: int | None = None):
    """Create the existing 10003 server with every legacy RPC still registered."""
    legacy_port = CONFIG["server"]["port"] if port is None else port
    workers = CONFIG["server"]["workers"]
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))
    rp_grpc.add_RecordProcessorServicer_to_server(RecordProcessorServicer(), server)
    emb_grpc.add_EmbeddingServiceServicer_to_server(EmbeddingServiceServicer(), server)
    chat_grpc.add_MirrorChatServicer_to_server(MirrorChatServicer(), server)
    tarot_grpc.add_TarotReadingServicer_to_server(TarotReadingServicer(), server)
    profile_grpc.add_MirrorProfileServicer_to_server(MirrorProfileServicer(), server)
    bound_port = server.add_insecure_port(f"[::]:{legacy_port}")
    if bound_port == 0:
        raise RuntimeError(f"could not bind legacy gRPC port {legacy_port}")
    return server, bound_port


def build_agent_server(*, port: int | None = None, servicer: AgentRunnerServicer | None = None):
    """Create the isolated Agent gRPC server with a hard RPC concurrency ceiling.

    The port comes from config.yml, overridable through MIRROR_AI_AGENT_PORT: hosts
    where 10004 is already taken (nginx sites, other services) keep config.yml untouched.
    """
    agent_config = CONFIG["agent"]
    configured_port = int(os.getenv("MIRROR_AI_AGENT_PORT", agent_config["port"]))
    agent_port = configured_port if port is None else port
    workers = int(agent_config["workers"])
    maximum = int(agent_config["maximum_concurrent_rpcs"])
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=workers),
        maximum_concurrent_rpcs=maximum,
    )
    instance = servicer or AgentRunnerServicer()
    agent_grpc.add_AgentRunnerServicer_to_server(instance, server)
    bound_port = server.add_insecure_port(f"[::]:{agent_port}")
    if bound_port == 0:
        raise RuntimeError(f"could not bind Agent gRPC port {agent_port}")
    return server, instance, bound_port


def serve():
    server, port = build_legacy_server()
    agent_server = None
    agent_servicer = None
    agent_port = None
    agent_enabled = bool(CONFIG["agent"].get("enabled", False)) or os.getenv("MIRROR_AI_AGENT_ENABLED", "").lower() in {
        "1", "true", "yes", "on",
    }
    if agent_enabled:
        if not os.getenv("MIRROR_AGENT_INTERNAL_TOKEN") or not os.getenv("MIRROR_AI_READ_DSN"):
            raise RuntimeError("Agent is enabled but MIRROR_AGENT_INTERNAL_TOKEN or MIRROR_AI_READ_DSN is missing")
        agent_server, agent_servicer, agent_port = build_agent_server()
        agent_server.start()
    server.start()

    print("=" * 50)
    print(f"Mirror AI 服务启动 | 端口: {port} | workers: {CONFIG['server']['workers']}")
    print("=" * 50)
    print("服务列表:")
    print("  - RecordProcessor  (Classify 含 single 单段模式, ExtractTerms 词条抽取)")
    print("  - EmbeddingService (Embed, EmbedBatch, GetModelInfo[健康检查])")
    print("  - MirrorChat       (ExtractIntent, Chat 流式, PlanTools 工具规划)")
    print("                    (PlanNextStep 循环规划, SummarizeDay 每日总结/记忆候选)")
    print("  - MirrorProfile    (GenerateProfile)")
    if agent_enabled:
        print(f"  - AgentRunner      (read-only RunAgent 流式, 端口 {agent_port}, 并发上限 2)")
    print("=" * 50)

    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        print("\n正在停止...")
        if agent_server is not None:
            agent_server.stop(0)
        server.stop(0)
        if agent_servicer is not None:
            agent_servicer.close()
        print("已停止")


if __name__ == '__main__':
    serve()
