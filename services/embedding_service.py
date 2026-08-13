"""EmbeddingService 服务实现"""

import grpc
from generated import embedding_pb2 as pb2
from generated import embedding_pb2_grpc as pb2_grpc

from embedding.factory import create_embedder


class EmbeddingServiceServicer(pb2_grpc.EmbeddingServiceServicer):

    def Embed(self, request, context):
        text = request.text
        config = request.embedding_config

        print(f"[Embed] 文本: {text}")
        print(f"[Embed] source={config.source}, provider={config.api_provider}, "
              f"model={config.api_model}, base_url={config.base_url}")

        try:
            embedder = create_embedder(
                source=config.source,
                local_model=config.local_model,
                api_provider=config.api_provider,
                api_key=config.api_key,
                api_model=config.api_model,
                base_url=config.base_url,
            )
            vector = embedder.embed(text)
            info = embedder.get_model_info()

            print(f"[Embed] 成功! dimension={info['dimension']}, model={info['model_name']}")
            print(f"[Embed] 向量前5维: {vector[:5]}")

            return pb2.EmbedResponse(
                vector=vector,
                dimension=info["dimension"],
                model_name=info["model_name"],
            )
        except Exception as e:
            print(f"[Embed] 错误: {e}")
            context.abort(grpc.StatusCode.INTERNAL, f"Embedding 失败: {str(e)}")

    def EmbedBatch(self, request, context):
        texts = request.texts
        config = request.embedding_config

        print(f"[EmbedBatch] 文本数量: {len(texts)}")

        try:
            embedder = create_embedder(
                source=config.source,
                local_model=config.local_model,
                api_provider=config.api_provider,
                api_key=config.api_key,
                api_model=config.api_model,
                base_url=config.base_url,
            )
            vectors = embedder.embed_batch(texts)
            info = embedder.get_model_info()

            results = [
                pb2.EmbedResponse(
                    vector=v,
                    dimension=info["dimension"],
                    model_name=info["model_name"],
                )
                for v in vectors
            ]
            return pb2.EmbedBatchResponse(results=results)
        except Exception as e:
            print(f"[EmbedBatch] 错误: {e}")
            context.abort(grpc.StatusCode.INTERNAL, f"Embedding 失败: {str(e)}")

    def GetModelInfo(self, request, context):
        print("[GetModelInfo] 收到请求")

        # 返回默认信息，实际应该根据配置查询
        return pb2.ModelInfoResponse(
            model_name="unknown",
            source="unknown",
            dimension=-1,
            available=True,
        )