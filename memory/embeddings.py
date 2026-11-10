"""情景记忆和画像共用的 embedding 配置及向量校验，不静默切换模型。"""
from __future__ import annotations
import math
import os
from collections.abc import Callable

class SiliconFlowEmbedding:
    def __init__(self, key: str, model: str, url: str) -> None:
        self.key, self.model, self.url = key, model, url

    def __call__(self, texts: list[str]) -> list[list[float]]:
        import httpx
        with httpx.Client(timeout=15) as client:
            response = client.post(self.url, headers={"Authorization": f"Bearer {self.key}"},
                                   json={"model": self.model, "input": texts, "encoding_format": "float"})
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            if [item["index"] for item in data] != list(range(len(texts))):
                raise ValueError("embedding 响应数量/顺序不合法")
            return [item["embedding"] for item in data]


def embedding_from_env() -> tuple[str, Callable]:
    provider = os.getenv("COMMERCEMIND_MEMORY_EMBEDDING_PROVIDER", "auto").strip()
    key = os.getenv("SILICONFLOW_API_KEY", "").strip()
    if provider == "auto":
        provider = "siliconflow" if key else "default"
    if provider == "siliconflow":
        if not key:
            raise ValueError("siliconflow embedding 缺少 Key")
        model = os.getenv("COMMERCEMIND_MEMORY_EMBEDDING_MODEL", "BAAI/bge-m3")
        return f"siliconflow:{model}", SiliconFlowEmbedding(key, model, os.getenv(
            "COMMERCEMIND_MEMORY_EMBEDDING_URL", "https://api.siliconflow.cn/v1/embeddings"))
    if provider == "default":
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
        function = DefaultEmbeddingFunction()
        return "chroma:all-MiniLM-L6-v2", lambda texts: function(texts)
    raise ValueError("embedding provider 只支持 auto/default/siliconflow")


def encode_vectors(embed: Callable, texts: list[str]) -> list[list[float]]:
    vectors = embed(texts)
    if len(vectors) != len(texts):
        raise ValueError("embedding 数量不符")
    result = [[float(x) for x in row] for row in vectors]
    if not result or not result[0] or any(len(row) != len(result[0]) or
            any(not math.isfinite(x) for x in row) or not any(row) for row in result):
        raise ValueError("embedding 无效")
    return result
