"""只读查看 ChromaDB 情景记忆；不加载 tokenizer/embedding、不下载模型。"""
import argparse
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def inspect(host: str, port: int, collection: str | None, limit: int, embeddings: bool):
    base = f"http://{host}:{port}/api/v1"
    def call(path, data=None):
        payload = None if data is None else json.dumps(data).encode()
        request = Request(base + path, data=payload, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    found = []
    query = "?" + urlencode({"tenant": "default_tenant", "database": "default_database"})
    for item in call("/collections" + query):
        if (collection and item["name"] != collection) or (not collection and not item["name"].startswith("episodic_")):
            continue
        path = "/collections/" + item["id"]
        include = ["documents", "metadatas"] + (["embeddings"] if embeddings else [])
        found.append({"collection": item["name"], "configuration": item["metadata"],
                      "count": call(path + "/count"),
                      "sample": call(path + "/get", {"limit": limit, "include": include})})
    return {"collections": found, "note": "count=0 表示该 collection 尚无 episode；空列表表示尚无匹配的 collection"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("CHROMA_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CHROMA_PORT", "8001")))
    parser.add_argument("--collection")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--embeddings", action="store_true", help="同时显示完整向量，输出可能很长")
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("limit 必须大于 0")
    print(json.dumps(inspect(args.host, args.port, args.collection, args.limit, args.embeddings), ensure_ascii=False, indent=2))
