"""本地 Embedding 实现（BGE-m3）"""

from embedding.base import BaseEmbedder


class LocalEmbedder(BaseEmbedder):
    """本地 Embedding — 使用 sentence-transformers 加载模型

    懒加载：首次使用时才下载并加载模型
    单例模式：同一模型只加载一次
    """

    _instances: dict[str, "LocalEmbedder"] = {}
    _models: dict[str, object] = {}

    def __new__(cls, model_name: str = "BAAI/bge-m3"):
        if model_name not in cls._instances:
            cls._instances[model_name] = super().__new__(cls)
        return cls._instances[model_name]

    def __init__(self, model_name: str = "BAAI/bge-m3"):
        if model_name not in self._models:
            self._load_model(model_name)
        self.model_name = model_name

    def _load_model(self, model_name: str):
        """懒加载模型"""
        from sentence_transformers import SentenceTransformer
        self._models[model_name] = SentenceTransformer(model_name)

    @property
    def _model(self):
        return self._models[self.model_name]

    def embed(self, text: str) -> list[float]:
        return self._model.encode(text).tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts).tolist()

    def get_model_info(self) -> dict:
        dimension = self._model.get_sentence_embedding_dimension()
        return {
            "model_name": self.model_name,
            "source": "local",
            "dimension": dimension,
            "available": True,
        }