"""Resume one authorized Fabric fetch inside the existing analysis receipt and tool ledger."""

import copy
from datetime import UTC, datetime

from bioradar.adapters.fabric_fetch import (
    FETCH_VERSION,
    normalize_fetch_request,
    poll_fetch,
    read_fetch_version,
    submit_fetch,
)
from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis import analysis_budget
from bioradar.research.investigations.analysis_models import AnalysisQueryReceipt
from bioradar.research.mining.models import canonical_hash


def _request(fixed, action):
    args = action["arguments"]
    scope = {
        "direction": fixed["query_policy"]["direction"],
        "mode": fixed["evidence_time"]["mode"],
        "as_of": fixed["evidence_time"]["cutoff"],
        "is_demo": fixed["is_demo"],
    }
    if (
        fixed["query_policy"].get("research_contract_version") != "bioradar-research-input-v1"
        or fixed["is_demo"]
        and not fixed.get("providers", {}).get("fabric_fetch", {}).get("demo_instance_id")
        or scope["mode"] != "current"
        or not fixed.get("providers", {}).get("fabric_fetch", {}).get("available")
        or args.get("scope") != scope
        or args.get("capability_id") != "fabric.history.fetch"
        or args.get("capability_version") != FETCH_VERSION
    ):
        raise DomainError("analysis_query_invalid", "主动补采仅允许已配置的本轮真实当前来源", 422)
    request = normalize_fetch_request(args.get("parameters"))
    if fixed["is_demo"]:
        request = normalize_fetch_request(
            {
                **request,
                "is_demo": True,
                "demo_instance_id": fixed["providers"]["fabric_fetch"]["demo_instance_id"],
            }
        )
    if request["source_id"] not in fixed["search_scope"]:
        raise DomainError("analysis_query_invalid", "主动补采来源不在本轮固定范围内", 422)
    return request


def _authorize(service, fixed, request):
    service._authorize_query_sources(
        fixed,
        {
            "kind": "source_fetch",
            "arguments": {
                "provider": "fabric",
                "source_id": request["source_id"],
            },
        },
    )


def _seconds(fixed, progress):
    return analysis_budget(fixed, progress, datetime.now(UTC))["usage"]["remaining_seconds"]


def _pending(service, identifier, token, progress, receipt):
    receipt["status"] = "prepared"
    receipt["usage"]["fetch"]["phase"] = "poll"
    service._checkpoint(identifier, token, progress)
    raise DomainError("analysis_fetch_pending", "补采运行仍在排队或采集中，已保存运行编号等待轮询", 409, True)


def _charge(service, progress, receipt, before, *, extra=0):
    count = service._tool_finish(progress, before, extra=extra)
    state = receipt["usage"]["fetch"]
    state["known_tool_calls"] = state.get("known_tool_calls", 0) + count
    receipt["cost"] = {"tool_calls": state["known_tool_calls"], "tokens": 0}
    return count


def _intent(receipt):
    state = receipt["usage"]["fetch"]
    receipt["status"] = "sent"
    receipt["cost"] = {
        "tool_calls": None,
        "known_tool_calls": state.get("known_tool_calls", 0),
        "tokens": 0,
        "reserved_tool_calls": 1,
    }


def execute_source_fetch(service, identifier, token, fixed, progress, action, step):
    """The host catches analysis_fetch_pending and resumes this same pending action later."""
    from bioradar.research.investigations.query_policy import semantic_key

    request = _request(fixed, action)
    receipt = next((q for q in progress["queries"] if q.get("action_id") == action["id"]), None)
    request_hash = canonical_hash(request)
    if receipt is None:
        if (
            fixed.get("query_policy", {}).get("schema_version") != "query-planner.external.v2"
            and len(progress["queries"]) >= fixed["budget"]["max_queries"]
        ):
            raise DomainError("analysis_query_budget", "本轮取证动作额度已用完", 422)
        receipt = AnalysisQueryReceipt(
            index=len(progress["queries"]),
            provider="fabric",
            kind="source_fetch",
            plan=copy.deepcopy(action["arguments"]),
            action_id=action["id"],
            semantic_key=semantic_key("source_fetch", action["arguments"]),
            decision_step=step,
            status="prepared",
            scope="isolated_simulation_fetch" if fixed["is_demo"] else "current_real_explicit_source_fetch",
            cost={"tool_calls": 0, "tokens": 0},
            usage={
                "request_status": "not_sent",
                "fetch": {
                    "request": copy.deepcopy(request),
                    "request_hash": request_hash,
                    "call_key": "analysis-fetch:" + canonical_hash([identifier, action["id"]]),
                    "submission_status": "prepared",
                    "run_id": None,
                    "phase": "submit",
                },
            },
        ).model_dump(mode="json")
        receipt["records"] = []
        progress["queries"].append(receipt)
        service._checkpoint(identifier, token, progress)
    state = (receipt.get("usage") or {}).get("fetch", {})
    if state.get("request_hash") != request_hash or state.get("request") != request:
        raise DomainError("analysis_input_changed", "已存补采动作的参数发生变化，未重新提交", 409)
    if receipt["status"] in {"failed", "unavailable", "empty", "unconfirmed"}:
        return receipt
    if state.get("poll_status") == "sent" or (state.get("version_intent") or {}).get("status") == "sent":
        receipt.update(status="unconfirmed", error_code="history_fetch_read_outcome_unconfirmed")
        service._checkpoint(identifier, token, progress)
        return receipt
    if not state.get("run_id"):
        if state.get("submission_status") in {"sent", "unknown"}:
            receipt.update(status="unconfirmed", error_code="history_fetch_outcome_unconfirmed")
            receipt["usage"]["request_status"] = "unknown"
            service._checkpoint(identifier, token, progress)
            return receipt
        _authorize(service, fixed, request)
        service._guard(identifier, token, check_deadline=True)
        if service.reader is None:
            receipt.update(status="unavailable", error_code="history_not_connected")
            service._checkpoint(identifier, token, progress)
            return receipt
        before = service._tool_start(fixed, progress)
        state["submission_status"] = "sent"
        _intent(receipt)
        service._checkpoint(identifier, token, progress)
        extra = 0
        try:
            service._guard(identifier, token, check_deadline=True)
            seconds = _seconds(fixed, progress)
            if seconds <= 0:
                raise DomainError("analysis_time_budget", "补采意图保存后时限已到，请求未发送", 409)
            result = submit_fetch(
                service.settings,
                request,
                call_key=state["call_key"],
                timeout_seconds=min(30, seconds),
                is_demo=fixed["is_demo"],
            )
            extra = result["tool_requests"]
            receipt["usage"]["request_status"] = result["usage"]["request_status"]
            state["submission_status"] = result["usage"]["request_status"]
            state["run_id"], state["run"] = result["run_id"], result["run"]
            receipt["error_code"] = result["error_code"]
            if result["status"] == "unconfirmed":
                receipt["status"] = "unconfirmed"
            elif not result["run_id"]:
                receipt["status"] = "unavailable" if result["status"] == "unavailable" else "failed"
            else:
                receipt["status"] = "prepared"
                state["phase"] = "poll" if result["status"] == "pending" else "read_versions"
        except DomainError as exc:
            state["submission_status"] = "not_sent"
            receipt["usage"]["request_status"] = "not_sent"
            receipt.update(status="failed", error_code=exc.code)
            raise
        finally:
            _charge(service, progress, receipt, before, extra=extra)
            service._checkpoint(identifier, token, progress)
        if receipt["status"] in {"unconfirmed", "failed", "unavailable"}:
            return receipt
        if state["phase"] == "poll":
            _pending(service, identifier, token, progress, receipt)
    elif state.get("phase") == "poll":
        _authorize(service, fixed, request)
        service._guard(identifier, token, check_deadline=True)
        before = service._tool_start(fixed, progress)
        state["poll_status"] = "sent"
        _intent(receipt)
        service._checkpoint(identifier, token, progress)
        try:
            service._guard(identifier, token, check_deadline=True)
            if _seconds(fixed, progress) <= 0:
                state["poll_status"] = "not_sent"
                raise DomainError("analysis_time_budget", "补采轮询时限已到，请求未发送", 409)
            result = poll_fetch(
                service.reader,
                state["run_id"],
                source_id=request["source_id"],
                is_demo=fixed["is_demo"],
                demo_instance_id=request.get("demo_instance_id"),
            )
            state["run"], state["poll_status"] = result["run"], "received"
            state["phase"] = "poll" if result["status"] == "pending" else "read_versions"
            receipt["status"] = "prepared"
        except DomainError as exc:
            state["poll_status"] = "failed"
            receipt.update(status="failed", error_code=exc.code)
            raise
        finally:
            _charge(service, progress, receipt, before)
            service._checkpoint(identifier, token, progress)
        if state["phase"] == "poll":
            _pending(service, identifier, token, progress, receipt)
    run = state["run"]
    receipt["has_more"] = run["state"] == "partial"
    if run["state"] not in {"completed", "partial"}:
        receipt.update(status="failed", error_code="history_fetch_" + run["state"])
        service._checkpoint(identifier, token, progress)
        return receipt
    state.setdefault("read_cutoff", datetime.now(UTC).isoformat())
    cutoff = datetime.fromisoformat(state["read_cutoff"])
    originals = receipt.setdefault("records", [])
    received = {item["version_id"] for item in originals}
    for version in run["version_ids"]:
        if version in received:
            continue
        _authorize(service, fixed, request)
        service._guard(identifier, token, check_deadline=True)
        before = service._tool_start(fixed, progress)
        state["version_intent"] = {"version_id": version, "status": "sent"}
        _intent(receipt)
        service._checkpoint(identifier, token, progress)
        try:
            service._guard(identifier, token, check_deadline=True)
            original = read_fetch_version(service.reader, run, version, cutoff=cutoff)
            originals.append(original)
            state["version_intent"]["status"] = "received"
            receipt["status"] = "prepared"
        except DomainError as exc:
            state["version_intent"]["status"] = "failed"
            receipt.update(status="failed", error_code=exc.code)
            raise
        finally:
            _charge(service, progress, receipt, before)
            service._checkpoint(identifier, token, progress)
    receipt.update(status="read" if originals else "empty", returned=len(originals))
    state["phase"] = "adopt"
    service._checkpoint(identifier, token, progress)
    for original in originals:
        ref = service.reader.ref(original).model_dump(mode="json")
        if any(item["record_ref"] == ref for item in progress["evidence"]):
            continue
        _authorize(service, fixed, request)
        try:
            entry = service._adopt_original(
                identifier,
                token,
                fixed,
                progress,
                original,
                "fetch-version:" + canonical_hash([action["id"], ref]),
            )
        except DomainError as exc:
            if exc.code != "analysis_evidence_budget":
                raise
            state["adoption_stop_reason"] = exc.code
            service._checkpoint(identifier, token, progress)
            break
        progress["evidence"].append(entry)
        receipt["evidence_keys"].append(entry["key"])
        receipt["new_versions"] += 1
        dependency = {
            "instance": service._source_instance(fixed["is_demo"]),
            "source_id": original["source_id"],
            "history": True,
        }
        if dependency not in fixed["dependency_sources"]:
            fixed["dependency_sources"].append(dependency)
        service._checkpoint(identifier, token, progress, fixed=fixed)
    state["phase"] = "completed"
    service._checkpoint(identifier, token, progress)
    return receipt


def make_topic_fetch_hook(settings, reader, *, guard=None):
    """Use the same submit/poll protocol in the topic service's existing durable receipt slot."""

    def hook(context, *, call_key, prior_receipt, checkpoint):
        previous = copy.deepcopy(prior_receipt) if prior_receipt is not None else None
        context_hash = canonical_hash(
            {
                key: context.get(key)
                for key in (
                    "topic_id",
                    "scope_revision",
                    "source_id",
                    "is_demo",
                    "terms_all",
                    "terms_any",
                    "since",
                    "until",
                    "mode",
                )
            }
        )
        if previous and (
            previous.get("context_hash", context_hash) != context_hash or previous.get("call_key") != call_key
        ):
            raise DomainError("history_fetch_input_changed", "主题补采范围与已保存意图不同", 409)
        if previous and previous.get("status") == "received":
            if (previous.get("result") or {}).get("status") != "pending":
                return previous
        if previous and not previous.get("run_id") and previous.get("status") in {"sent", "unknown"}:
            previous.update(status="unknown", reason="history_fetch_outcome_unconfirmed")
            checkpoint(copy.deepcopy(previous))
            return previous
        from bioradar.adapters.fabric_fetch import demo_fetch_available

        is_demo = bool(context.get("is_demo"))
        unsupported = (
            is_demo
            and not demo_fetch_available(settings)
            or context.get("mode", "current") != "current"
            or context.get("source_id")
            not in {
                "clinicaltrials",
                "pubmed",
                "fda_crl",
                "europe_pmc",
                "who_don",
                "sec_companyfacts",
                *(["external_archive"] if is_demo else []),
            }
        )
        unconfigured = not (previous or {}).get("run_id") and (
            not getattr(settings, "data_fabric_history_fetch_token", None)
            or not getattr(settings, "data_fabric_url", None)
        )
        if unsupported or unconfigured:
            receipt = previous or {
                "request_hash": context_hash,
                "context_hash": context_hash,
                "call_key": call_key,
                "run_id": None,
                "tool_requests": 0,
            }
            receipt.update(status="not_sent", reason="topic_fetch_not_supported")
            checkpoint(copy.deepcopy(receipt))
            return receipt
        if previous and previous.get("status") == "received":
            result = previous.get("result") or {}
            if result.get("status") != "pending":
                return previous
        request = normalize_fetch_request(
            {
                "source_id": context["source_id"],
                "query": " ".join([*context.get("terms_all", []), *context.get("terms_any", [])])
                or ("*" if is_demo else ""),
                "max_records": 10,
                "max_pages": 1,
                "deadline_seconds": 30,
                **({"updated_since": context["since"]} if context.get("since") and not is_demo else {}),
                **({"updated_until": context["until"]} if context.get("until") and not is_demo else {}),
                **(
                    {"is_demo": True, "demo_instance_id": settings.data_fabric_demo_fetch_instance_id}
                    if is_demo
                    else {}
                ),
            }
        )
        request_hash = canonical_hash(request)
        if previous and (
            previous.get("request_hash") != request_hash or previous.get("call_key") != call_key
        ):
            raise DomainError("history_fetch_input_changed", "主题补采参数与已保存意图不同", 409)
        receipt = previous or {
            "status": "prepared",
            "request": request,
            "request_hash": request_hash,
            "call_key": call_key,
            "context_hash": context_hash,
            "run_id": None,
            "tool_requests": 0,
            "query_qualification": (
                "模拟补采读取当前可见包；补采不执行时间筛选，since/until仍由随后专题原件读取应用。"
                if is_demo
                else "上游关键词取数；不代表本地all/any匹配或全源完成。"
            ),
        }
        if not receipt.get("run_id") and receipt["status"] in {"sent", "unknown"}:
            receipt.update(status="unknown", reason="history_fetch_outcome_unconfirmed")
            checkpoint(copy.deepcopy(receipt))
            return receipt
        if not context.get("may_send", False):
            receipt["reason"] = "topic_fetch_send_not_admitted"
            checkpoint(copy.deepcopy(receipt))
            return receipt
        if guard is not None:
            guard(context)
        before = reader.requests_made
        if not receipt.get("run_id"):
            receipt["status"] = "sent"
            checkpoint(copy.deepcopy(receipt))
            if guard is not None:
                try:
                    guard(context)
                except DomainError as exc:
                    receipt.update(status="not_sent", reason=exc.code)
                    checkpoint(copy.deepcopy(receipt))
                    raise
            result = submit_fetch(settings, request, call_key=call_key, timeout_seconds=30, is_demo=is_demo)
        else:
            receipt["poll_status"] = "sent"
            checkpoint(copy.deepcopy(receipt))
            if guard is not None:
                guard(context)
            try:
                result = poll_fetch(
                    reader,
                    receipt["run_id"],
                    source_id=request["source_id"],
                    is_demo=is_demo,
                    demo_instance_id=request.get("demo_instance_id"),
                )
            except DomainError as exc:
                receipt.update(status="error", reason=exc.code)
                receipt["tool_requests"] += max(0, reader.requests_made - before)
                checkpoint(copy.deepcopy(receipt))
                raise
        receipt["tool_requests"] += result["tool_requests"] + max(0, reader.requests_made - before)
        receipt.update(
            status="unknown"
            if result["status"] == "unconfirmed"
            else "not_sent"
            if result["usage"]["request_status"] == "not_sent"
            else "received",
            run_id=result["run_id"],
            result=copy.deepcopy(result),
            request_status=result["usage"]["request_status"],
            reason=result["error_code"],
        )
        checkpoint(copy.deepcopy(receipt))
        return receipt

    return hook
