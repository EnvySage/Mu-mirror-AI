"""Embedding 模块 — 统一接口，支持本地/API 切换"""

from embedding.base import BaseEmbedder
from embedding.factory import create_embedder

__all__ = ["BaseEmbedder", "create_embedder"]