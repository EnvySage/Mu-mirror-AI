"""EmbeddingService 服务实现"""

import grpc
from generated import embedding_pb2 as pb2
from generated import embedding_pb2_grpc as pb2_grpc

from embedding.factory import create_embedder
from errors import abort_with_mapped


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

    # 维度探测用文本（api 模式需真实调用一次才能确定维度）
    _DIMENSION_PROBE_TEXT = "dimension probe"

    def GetModelInfo(self, request, context):
        """模型信息查询（双语义，见 shared-protocol.md 2026-09-04 登记）

        1. 携带 embedding_config 时：按用户配置构造 embedder，返回该模型的
           dimension / model_name，供 Java 侧做 1024 维硬约束（裁决 #18）。
           - source=local：仍用本地 BGE-m3（懒加载+单例缓存），返回真实维度（1024）。
           - source=api：真实调用一次 embedding API 探测维度（get_model_info 静态
             无法得知维度）。该路径仅由 Java 侧"测试连接"触发，非热路径。
           配置错误（key 无效/不可达）按 #8 映射为 UNAVAILABLE 等状态码。
        2. 未携带（字段缺省）：健康检查语义（协作清单 #9，Docker healthcheck 用），
           行为与历史版本完全一致——不加载模型、不调用任何 API，只确认 gRPC 通路。

        无状态契约不破坏：本方法不缓存任何用户配置（LocalEmbedder 的模型单例除外，
        与 Embed 路径共享）。
        """
        config = request.embedding_config
        has_config = request.HasField("embedding_config")

        if not has_config or not config.source:
            print("[GetModelInfo] health check (no embedding_config)")
            return pb2.ModelInfoResponse(
                model_name="mirror-ai",
                source="service",
                dimension=-1,  # 维度取决于请求时的 EmbeddingConfig，此处无上下文
                available=True,
            )

        print(f"[GetModelInfo] source={config.source}, api_provider={config.api_provider}, "
              f"api_model={config.api_model}, base_url={config.base_url}, "
              f"local_model={config.local_model}")

        try:
            embedder = create_embedder(
                source=config.source,
                local_model=config.local_model,
                api_provider=config.api_provider,
                api_key=config.api_key,
                api_model=config.api_model,
                base_url=config.base_url,
            )
            info = embedder.get_model_info()
            if info.get("dimension", -1) <= 0:
                # api 模式：维度只能实测（顺带完成连通性/鉴权校验）
                vector = embedder.embed(self._DIMENSION_PROBE_TEXT)
                info["dimension"] = len(vector)

            print(f"[GetModelInfo] model={info['model_name']}, "
                  f"dimension={info['dimension']}, available={info['available']}")

            return pb2.ModelInfoResponse(
                model_name=info["model_name"],
                source=info["source"],
                dimension=info["dimension"],
                available=info["available"],
            )
        except Exception as e:
            print(f"[GetModelInfo] 错误: {e}")
            abort_with_mapped(context, e)