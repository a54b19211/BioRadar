"""What Bio-Planner is asked each round and what is kept of its answer (task book 04, 2.5; ADR 0013).

Every call of the planner leaves one record in `progress.decisions`: the request without its history, a
reference to the history (how many actions, and their hash), the hash of the whole request, the whole answer,
which planner answered, and the action that followed. The history is not stored: actions are only ever
appended, so `rebuilt_request` puts the request together again from the saved actions, and its hash tells
whether it is the request that was sent. A planner call that failed, and the call that made the run stop, are
records too. All records of an analysis together stay within MAX_BYTES; a round that would not fit keeps only
its hashes and says so (snapshot_truncated): it can no longer be rebuilt."""

import hashlib
import json
from typing import TYPE_CHECKING

from bioradar.adapters import services
from bioradar.adapters.services import ServiceError
from bioradar.research.run import Failed
from bioradar.research.run.search import LITERAL, SEMANTIC, channel_of

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run


MAX_BYTES = 1024 * 1024
ROOM_FOR_CUT = 32 * 1024  # what the short records of the rounds still to come need (31 rounds at most)
CHANNELS = (LITERAL, SEMANTIC, "gdelt_leads", "graph")  # the planner's channels; each is declared every round


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def history(actions) -> list[dict]:
    """The actions done, as Bio-Planner's history. Channels go on the history exactly as on the candidates,
    or the planner does not recognise an action as done."""
    return [history_item(a) for a in actions]


def history_item(action) -> dict:
    item = {
        "kind": action["kind"],
        "source": action["source"],
        "target_checks": action["target_checks"],
        "gain": action["gain"],
        "args": dict(action["args"]),
        "status": action["status"],
    }
    if channel := channel_of(action):
        item["channel"] = channel
    if "evidence_gain" in action:  # what a read found apart from answering checks
        item["evidence_gain"] = action["evidence_gain"]
    return item


def channels(run: "Run") -> list[dict]:
    """What the radar says about each retrieval channel this round; the planner never chooses through one
    declared unavailable. Reads have no channel: a read is Fabric's read of a record already found."""

    def declare(channel, reason=""):
        return {"channel": channel, "available": not reason, "reason": reason}

    return [
        declare(LITERAL),
        declare(SEMANTIC, "" if run.s.semantic_search else "not_configured"),
        declare("gdelt_leads", "not_supported"),  # the radar offers no GDELT lead list yet
        declare("graph", "03_not_run"),  # the graph package of task book 03 does not exist yet
    ]


def planner_snapshot(run: "Run") -> dict:
    """Ask the planner what it accepts (GET /health), once when the stage starts, and keep the answer with
    every record. A planner that does not list its channels is older than the request the radar sends (it
    would refuse the new fields with 422): the run stops and says so, it does not send less."""
    try:
        health = services.planner_health(run.s)
    except ServiceError as exc:
        raise Failed(f"planner_{exc.code}") from exc
    listed = health.get("channels")
    if not isinstance(listed, list) or not set(CHANNELS) <= set(listed):
        run.note("planner_outdated", "planner /health does not list the channels; deploy planner 0.2.0 first")
        run.save()
        raise Failed("planner_outdated")
    return {"version": health.get("version"), "kinds": health.get("kinds"), "channels": listed}


def keep(run: "Run", request: dict, planner: dict, *, response=None, action_index=None, stop=None):
    """Add the record of one planner call. action_index: the index the action that followed will have; stop:
    why no action followed (the planner's stop reason, planner_<error code>, llm_call_limit)."""
    kept = run.p.setdefault("decisions", [])
    entry = {
        "step": len(kept) + 1,
        "history_ref": {"actions": len(request["history"]), "sha256": digest(request["history"])},
        "request_sha256": digest(request),
        "planner": planner,
        "action_index": action_index,
    }
    if stop:
        entry["stop"] = stop
    full = entry | {"request": {k: v for k, v in request.items() if k != "history"}}
    if response is not None:
        full["response"] = response
    used = sum(len(canonical(e).encode()) for e in kept)
    if used + len(canonical(full).encode()) > MAX_BYTES - ROOM_FOR_CUT:
        full = entry | {"snapshot_truncated": True}
    kept.append(json.loads(canonical(full)))  # what is stored is what was hashed, shared with nothing


def rebuilt_request(entry: dict, actions) -> dict | None:
    """The whole request of a kept round, put together again from the saved actions; its digest is the
    record's request_sha256. None for a round that was cut (snapshot_truncated)."""
    if entry.get("snapshot_truncated"):
        return None
    return {**entry["request"], "history": history(actions[: entry["history_ref"]["actions"]])}
