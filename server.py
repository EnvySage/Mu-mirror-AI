"""Mirror AI gRPC 服务入口"""

import grpc
from concurrent import futures
import time

from generated import record_processor_pb2_grpc as rp_grpc
from generated import embedding_pb2_grpc as emb_grpc
from generated import mirror_chat_pb2_grpc as chat_grpc
from generated import mirror_profile_pb2_grpc as profile_grpc

from services.record_processor import RecordProcessorServicer
from services.embedding_service import EmbeddingServiceServicer
from services.chat_service import MirrorChatServicer
from services.profile_service import MirrorProfileServicer


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))

    # 注册所有服务
    rp_grpc.add_RecordProcessorServicer_to_server(RecordProcessorServicer(), server)
    emb_grpc.add_EmbeddingServiceServicer_to_server(EmbeddingServiceServicer(), server)
    chat_grpc.add_MirrorChatServicer_to_server(MirrorChatServicer(), server)
    profile_grpc.add_MirrorProfileServicer_to_server(MirrorProfileServicer(), server)

    port = 50051
    server.add_insecure_port(f'[::]:{port}')
    server.start()

    print("=" * 50)
    print(f"Mirror AI 服务启动 | 端口: {port}")
    print("=" * 50)
    print("服务列表:")
    print("  - RecordProcessor  (Classify, Split)")
    print("  - EmbeddingService (Embed, EmbedBatch, GetModelInfo)")
    print("  - MirrorChat       (ExtractIntent, Chat)")
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