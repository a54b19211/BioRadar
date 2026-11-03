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
    """Return (hits, scopes). hits: [{source, record_id, version_id, score}] for bridge hits that carry a
    Fabric identity. scopes: per scope, which channels actually ran (keyword/vector/embedding/rerank,
    degraded) or the error, so one failed scope keeps the others' hits and a keyword-only fallback is
    never mistaken for a semantic search. The bridge has no cutoff: callers must not use it in a replay."""
    if not settings.weknora_url:
        return [], {}
    http = client or httpx.Client(timeout=60)
    hits, seen, scopes = [], set(), {}
    for scope in SCOPES.get(direction, ("research",)):
        try:
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
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            scopes[scope] = {"error": f"http_{status}" if status else type(exc).__name__}
            continue
        execution = data.get("execution") or {}
        scopes[scope] = {k: execution.get(k) for k in ("keyword", "vector", "embedding", "rerank", "degraded")}
        for hit in data.get("results") or []:
            key = identity(hit)
            if key and key not in seen:
                seen.add(key)
                version = (hit.get("identity") or {}).get("version_id")
                hits.append({"source": key[0], "record_id": key[1], "version_id": version, "score": hit.get("score")})
    return hits, scopes
