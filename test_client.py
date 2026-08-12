"""测试客户端 — 验证 gRPC 连接和服务"""

import grpc

from generated import record_processor_pb2 as rp_pb2
from generated import record_processor_pb2_grpc as rp_grpc
from generated import embedding_pb2 as emb_pb2
from generated import embedding_pb2_grpc as emb_grpc
from generated import common_pb2


def test_record_processor():
    print("\n" + "=" * 50)
    print("RecordProcessor 服务测试")
    print("=" * 50)

    with grpc.insecure_channel('localhost:50051') as channel:
        stub = rp_grpc.RecordProcessorStub(channel)

        llm_config = common_pb2.LlmConfig(
            provider="openai",
            api_key="test-key",
            base_url="",
            model="gpt-4o-mini"
        )

        # Classify
        print("\n[Classify]")
        try:
            resp = stub.Classify(rp_pb2.ClassifyRequest(
                content="今天学习了 Python gRPC 编程",
                llm_config=llm_config
            ))
            print(f"  ✓ title: {resp.title}")
            print(f"  ✓ summary: {resp.summary}")
            print(f"  ✓ content_type: {resp.content_type}")
            print(f"  ✓ keywords: {list(resp.keywords)}")
        except grpc.RpcError as e:
            print(f"  ✗ {e.code()}: {e.details()}")

        # Split
        print("\n[Split]")
        try:
            resp = stub.Split(rp_pb2.SplitRequest(
                content="今天上午开会，下午写代码，晚上跑步",
                llm_config=llm_config
            ))
            print(f"  ✓ need_split: {resp.need_split}")
        except grpc.RpcError as e:
            print(f"  ✗ {e.code()}: {e.details()}")


def test_embedding_service():
    print("\n" + "=" * 50)
    print("EmbeddingService 服务测试")
    print("=" * 50)

    with grpc.insecure_channel('localhost:50051') as channel:
        stub = emb_grpc.EmbeddingServiceStub(channel)

        # GetModelInfo
        print("\n[GetModelInfo]")
        try:
            resp = stub.GetModelInfo(emb_pb2.ModelInfoRequest())
            print(f"  ✓ model: {resp.model_name}")
            print(f"  ✓ source: {resp.source}")
            print(f"  ✓ dimension: {resp.dimension}")
            print(f"  ✓ available: {resp.available}")
        except grpc.RpcError as e:
            print(f"  ✗ {e.code()}: {e.details()}")

        # Embed
        print("\n[Embed]")
        try:
            embedding_config = common_pb2.EmbeddingConfig(
                source="local",
                local_model="BAAI/bge-m3",
                api_provider="",
                api_key="",
                api_model=""
            )
            resp = stub.Embed(emb_pb2.EmbedRequest(
                text="测试文本转向量",
                embedding_config=embedding_config
            ))
            print(f"  ✓ dimension: {resp.dimension}")
            print(f"  ✓ model: {resp.model_name}")
            print(f"  ✓ vector[:5]: {list(resp.vector[:5])}")
        except grpc.RpcError as e:
            print(f"  ✗ {e.code()}: {e.details()}")


def main():
    print("=" * 50)
    print("Mirror AI 测试客户端")
    print("=" * 50)
    print("确保 server.py 已启动")

    # 测试连接
    try:
        with grpc.insecure_channel('localhost:50051') as channel:
            grpc.channel_ready_future(channel).result(timeout=5)
            print("\n✓ 连接成功")
    except grpc.FutureTimeoutError:
        print("\n✗ 连接超时，请先启动 server.py")
        return

    test_record_processor()
    test_embedding_service()

    print("\n" + "=" * 50)
    print("测试完成")
    print("=" * 50)


if __name__ == '__main__':
    main()