"""WeKnora semantic search (李明洋).

A hit is only a pointer: the text is re-read from Fabric at the fixed version."""

import json

import httpx

from bioradar.config import Settings


def identity(hit: dict) -> tuple[str, str] | None:
    """(source_id, source_record_id) published by Fabric into the knowledge metadata, if present.
    The weknora-kdd bridge keeps it as a JSON string in raw_locator. Documents uploaded without a
    Fabric identity (the old crawler knowledge base) have none and are skipped."""
    for holder in (
        hit.get("metadata"),
        hit.get("custom_metadata"),
        (hit.get("knowledge") or {}).get("metadata"),
    ):
        if not isinstance(holder, dict):
            continue
        if holder.get("source_id") and holder.get("source_record_id"):
            return holder["source_id"], holder["source_record_id"]
        try:
            locator = json.loads(holder.get("raw_locator") or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(locator, dict) and locator.get("source_id") and locator.get("source_record_id"):
            return locator["source_id"], locator["source_record_id"]
    return None


def search(settings: Settings, query: str, *, top_k: int = 10, client: httpx.Client | None = None):
    """Return [(source_id, source_record_id, score)] for hits that carry a Fabric identity."""
    if not settings.weknora_url:
        return []
    http = client or httpx.Client(timeout=30)
    response = http.post(
        settings.weknora_url.rstrip("/") + "/api/v1/knowledge-search",
        headers={"X-API-Key": settings.weknora_token},
        # v0.9 fields; hybrid search (keyword-only search fails on ParadeDB in v0.9.0).
        json={"query": query, "knowledge_base_id": settings.weknora_knowledge_base_id, "match_count": top_k},
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
