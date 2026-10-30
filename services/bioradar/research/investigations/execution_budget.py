"""Host-owned reading limits and automatic daily reservations in the existing ledger."""

import copy
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from bioradar.api.errors import DomainError

WAIT_CODE = "analysis_automatic_budget_wait"


def reading_limits(fixed):
    """Old inputs retain their original ten-version, forty-thousand-character limit."""
    if fixed.get("query_policy", {}).get("research_contract_version") == "bioradar-research-input-v1":
        return fixed.get("reading_limits", {"max_versions": 32, "max_characters": 100000, "search_limit": 8})
    return {"max_versions": 10, "max_characters": 40000, "search_limit": 3}


def automatic_policy(fixed):
    policy = fixed.get("execution_budget", {})
    if policy.get("version") != "automatic-buckets-v1" or policy.get("pool") == "manual":
        return None
    return policy


def waiting_seconds(fixed, progress, now):
    """Only durable host-created automatic-budget waits pause the execution clock."""
    if automatic_policy(fixed) is None or not progress.get("started_at"):
        return 0
    lower = datetime.fromisoformat(progress["started_at"])
    intervals = list(progress.get("budget_wait_intervals", []))
    if progress.get("budget_wait"):
        intervals.append({**progress["budget_wait"], "ended_at": now.isoformat()})
    seconds = 0
    previous_end = lower
    for interval in intervals:
        if interval.get("reason") != WAIT_CODE:
            continue
        start = datetime.fromisoformat(interval["started_at"])
        end = datetime.fromisoformat(interval["ended_at"])
        if start.tzinfo is None or end.tzinfo is None or end < start:
            raise DomainError("analysis_budget_wait_invalid", "自动预算等待记录无效", 409)
        # Clipping avoids counting overlapping intervals twice.
        start, end = max(start, lower, previous_end), min(end, now)
        seconds += max(0, (end - start).total_seconds())
        previous_end = max(previous_end, end)
    return seconds


def finish_budget_wait(progress, now):
    wait = progress.get("budget_wait")
    if not wait or datetime.fromisoformat(wait["next_budget_at"]) > now:
        return False
    progress.setdefault("budget_wait_intervals", []).append({**wait, "ended_at": now.isoformat()})
    progress.pop("budget_wait", None)
    return True


def _charge(row):
    if row["receipt_status"] == "not_sent":
        return 0
    usage = row["usage"] or {}
    amount = usage.get("total_tokens")
    if amount is None and all(usage.get(key) is not None for key in ("prompt_tokens", "completion_tokens")):
        amount = usage["prompt_tokens"] + usage["completion_tokens"]
    if amount is not None:
        return max(0, int(amount))
    return max(0, int(row["upper_bound"] or 0))


def reserve_automatic_call(service, identifier, token, fixed, progress, call, now):
    """Serialize daily reservations before sending; confirmed receipts replay above this hook."""
    policy = automatic_policy(fixed)
    if policy is None:
        return
    from bioradar.research.investigations.analysis_service import InvestigationAnalysisRecord

    now = now.astimezone(UTC)
    day = now.date().isoformat()
    tomorrow = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat()
    required = call["token_upper_bound"]
    global_limit = min(policy["automatic_daily_tokens"], service.settings.automatic_daily_model_tokens)
    topic_id = policy.get("topic_id")
    topic_limit = min(policy["topic_daily_tokens"], service.settings.topic_daily_model_tokens)
    if required > global_limit or topic_id and required > topic_limit:
        raise DomainError(
            "analysis_automatic_request_budget",
            "单次请求预留超过自动日额度；需缩小输入或明确调整配置，未发送请求",
            422,
        )
    wait = False
    with Session(service.engine.database) as session, session.begin():
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "bioradar:auto-budget:" + service.actor.workspace_id},
        )
        row = session.get(InvestigationAnalysisRecord, identifier, with_for_update=True)
        if (
            row is None
            or row.execution_token != token
            or row.state != "running"
            or row.lease_until is None
            or row.lease_until <= now
        ):
            raise DomainError("analysis_execution_lost", "自动预算预留时执行资格已变化", 409)
        entries = session.execute(
            text("""
                SELECT a.id AS analysis_id,
                       a.fixed_input #>> '{execution_budget,topic_id}' AS topic_id,
                       c->>'call_id' AS call_id,
                       c->'receipt'->>'status' AS receipt_status,
                       c->'receipt'->'usage' AS usage,
                       c->>'token_upper_bound' AS upper_bound
                FROM investigation_analyses a
                CROSS JOIN LATERAL jsonb_array_elements(COALESCE(a.progress->'calls', '[]'::jsonb)) c
                WHERE a.workspace_id=:workspace
                  AND a.fixed_input #>> '{execution_budget,version}'='automatic-buckets-v1'
                  AND a.fixed_input #>> '{execution_budget,pool}'<>'manual'
                  AND c->'automatic_reservation'->>'day'=:day
            """),
            {"workspace": service.actor.workspace_id, "day": day},
        ).mappings()
        global_used = topic_used = 0
        for entry in entries:
            if entry["analysis_id"] == identifier and entry["call_id"] == call["call_id"]:
                continue
            amount = _charge(entry)
            global_used += amount
            if topic_id and entry["topic_id"] == topic_id:
                topic_used += amount
        progress["automatic_budget"] = {
            "day": day,
            "timezone": "UTC",
            "global_limit": global_limit,
            "global_used_before_this_call": global_used,
            "topic_id": topic_id,
            "topic_limit": topic_limit if topic_id else None,
            "topic_used_before_this_call": topic_used if topic_id else None,
            "requested_upper_bound": required,
            "qualification": "实际用量按回执计；缺用量及在途调用保守预留，预留不冒充已消费。",
        }
        wait = global_used + required > global_limit or bool(topic_id and topic_used + required > topic_limit)
        if wait:
            progress["budget_wait"] = {
                "reason": WAIT_CODE,
                "pool": policy["pool"],
                "started_at": now.isoformat(),
                "next_budget_at": tomorrow,
            }
        else:
            call["automatic_reservation"] = {"day": day, "topic_id": topic_id, "upper_bound": required}
            progress.pop("budget_wait", None)
        progress["updated_at"] = now.isoformat()
        row.progress = copy.deepcopy(progress)
    if wait:
        raise DomainError(WAIT_CODE, "自动日额度已用完，保留回执并等待下一预算日", 429)
