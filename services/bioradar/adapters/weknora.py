"""WeKnora semantic search through the weknora-kdd bridge (李明洋).

The bridge only returns documents Fabric published at a fixed version, scoped by business area, and says
which keyword / vector / rerank channels actually ran. A hit is only a pointer: the text is re-read from
Fabric at the analysis cutoff, so documents not yet visible then drop out there."""

import json

import httpx

from bioradar.config import Settings


def identity(hit: dict) -> tuple[str, str] | None:
    """(source_id, source_record_id) published by Fabric into the knowledge metadata, if present.
    The weknora-kdd bridge keeps it as a JSON string in raw_locator. Documents uploaded without a
    Fabric identity (the old crawler knowledge base) have none and are skipped."""
    for holder in (
        hit.get("identity"),
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


# Bridge source scopes searched per analysis direction (configured in the bridge's source mappings).
SCOPES = {
    "pipeline": ("pipeline", "research"),
    "science": ("research",),
    "business": ("business", "pipeline"),
    "public_health": ("public_health", "research"),
}


def search(
    settings: Settings,
    query: str,
    *,
    direction: str,
    is_demo: bool = False,
    top_k: int = 10,
    client: httpx.Client | None = None,
):
    """Return [(source_id, source_record_id, score)] for bridge hits that carry a Fabric identity."""
    if not settings.weknora_url:
        return []
    http = client or httpx.Client(timeout=60)
    result, seen = [], set()
    for scope in SCOPES.get(direction, ("research",)):
        response = http.post(
            settings.weknora_url.rstrip("/") + "/api/v1/kdd/search",
            headers={"Authorization": f"Bearer {settings.weknora_token}"},
            json={
                "knowledge_base_id": settings.weknora_knowledge_base_id,
                "source_scope": scope,
                "query_text": query,
                "top_k": top_k,
                "is_demo": is_demo,
            },
        )
        response.raise_for_status()
        for hit in response.json().get("results") or []:
            key = identity(hit)
            if key and key not in seen:
                seen.add(key)
                result.append((*key, hit.get("score")))
    return result
