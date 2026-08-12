"""EmbeddingService 服务实现"""

from generated import embedding_pb2 as pb2
from generated import embedding_pb2_grpc as pb2_grpc


class EmbeddingServiceServicer(pb2_grpc.EmbeddingServiceServicer):

    def __init__(self):
        self.model_name = "test-model"
        self.dimension = 1024

    def Embed(self, request, context):
        text = request.text
        embedding_config = request.embedding_config

        print(f"[Embed] 文本: {text}")
        print(f"[Embed] Source: {embedding_config.source}")

        fake_vector = [0.1] * self.dimension

        return pb2.EmbedResponse(
            vector=fake_vector,
            dimension=self.dimension,
            model_name=self.model_name
        )

    def EmbedBatch(self, request, context):
        texts = request.texts
        print(f"[EmbedBatch] 文本数量: {len(texts)}")

        results = []
        for _ in texts:
            fake_vector = [0.1] * self.dimension
            results.append(pb2.EmbedResponse(
                vector=fake_vector,
                dimension=self.dimension,
                model_name=self.model_name
            ))

        return pb2.EmbedBatchResponse(results=results)

    def GetModelInfo(self, request, context):
        print("[GetModelInfo] 收到请求")

        return pb2.ModelInfoResponse(
            model_name=self.model_name,
            source="local",
            dimension=self.dimension,
            available=True
        )