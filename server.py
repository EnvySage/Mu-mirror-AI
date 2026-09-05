"""Mirror AI gRPC 服务入口"""

import time
from concurrent import futures

import grpc

from config import CONFIG
from generated import record_processor_pb2_grpc as rp_grpc
from generated import embedding_pb2_grpc as emb_grpc
from generated import mirror_chat_pb2_grpc as chat_grpc
from generated import mirror_profile_pb2_grpc as profile_grpc

from services.lexicon_service import RecordProcessorServicer
from services.embedding_service import EmbeddingServiceServicer
from services.chat_service import MirrorChatServicer
from services.profile_service import MirrorProfileServicer


def serve():
    port = CONFIG["server"]["port"]
    workers = CONFIG["server"]["workers"]

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))

    # 注册所有服务
    # RecordProcessor 用 lexicon_service 版本：Classify（继承自 record_processor）+ ExtractTerms
    rp_grpc.add_RecordProcessorServicer_to_server(RecordProcessorServicer(), server)
    emb_grpc.add_EmbeddingServiceServicer_to_server(EmbeddingServiceServicer(), server)
    chat_grpc.add_MirrorChatServicer_to_server(MirrorChatServicer(), server)
    profile_grpc.add_MirrorProfileServicer_to_server(MirrorProfileServicer(), server)

    server.add_insecure_port(f'[::]:{port}')
    server.start()

    print("=" * 50)
    print(f"Mirror AI 服务启动 | 端口: {port} | workers: {workers}")
    print("=" * 50)
    print("服务列表:")
    print("  - RecordProcessor  (Classify 含 single 单段模式, ExtractTerms 词条抽取)")
    print("  - EmbeddingService (Embed, EmbedBatch, GetModelInfo[健康检查])")
    print("  - MirrorChat       (ExtractIntent, Chat 流式)")
    print("  - MirrorProfile    (GenerateProfile)")
    print("=" * 50)

    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        print("\n正在停止...")
        server.stop(0)
        print("已停止")


if __name__ == '__main__':
    serve()
