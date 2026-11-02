"""WeKnora semantic search (李明洋).

A hit is only a pointer: the text is re-read from Fabric at the fixed version."""

import httpx

from bioradar.config import Settings


def identity(hit: dict) -> tuple[str, str] | None:
    """(source_id, source_record_id) published by Fabric into the knowledge metadata, if present."""
    for holder in (
        hit.get("metadata"),
        hit.get("custom_metadata"),
        (hit.get("knowledge") or {}).get("metadata"),
    ):
        if isinstance(holder, dict) and holder.get("source_id") and holder.get("source_record_id"):
            return holder["source_id"], holder["source_record_id"]
    return None


def search(settings: Settings, query: str, *, top_k: int = 10, client: httpx.Client | None = None):
    """Return [(source_id, source_record_id, score)] for hits that carry a Fabric identity."""
    if not settings.weknora_url:
        return []
    http = client or httpx.Client(timeout=30)
    response = http.post(
        settings.weknora_url.rstrip("/") + "/api/v1/knowledge-search",
        headers={"X-API-Key": settings.weknora_token},
        json={"query": query, "knowledge_base_id": settings.weknora_knowledge_base_id, "top_k": top_k},
    )
    response.raise_for_status()
    data = response.json().get("data") or []
    result, seen = [], set()
    for hit in data:
        key = identity(hit)
        if key and key not in seen:
            seen.add(key)
            result.append((*key, hit.get("score")))
    return result
