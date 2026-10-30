"""Professional-method integration using the existing research state and model ledger."""

from __future__ import annotations

import copy

import httpx

from bioradar.api.errors import DomainError
from bioradar.research.mining.models import canonical_hash


def _configuration(settings, name):
    return (
        getattr(settings, name + "_transport", "local"),
        getattr(settings, name + "_service_url", None),
        getattr(settings, name + "_service_token", None),
    )


def provider_status(settings, name):
    from bioradar.adapters import biocheck, mech_evidence
    from bioradar.adapters.professional_rpc import get_capabilities

    mode, url, token = _configuration(settings, name)
    if mode == "local":
        result = (biocheck if name == "biocheck" else mech_evidence).provider_status(settings)
        return {**result, "transport": "local"}
    status = {
        "provider": name,
        "transport": "http",
        "configured": bool(url and token),
        "available": False,
        "status": "unavailable",
        "version": None,
        "service_endpoint": url,
        "error_code": "professional_rpc_not_configured",
        "qualification": "服务能力可读不代表本轮已执行；实际步骤、用量与核验结果另行保存。",
    }
    if not url or not token:
        return status
    try:
        with httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + token.get_secret_value()},
            timeout=min(10, settings.professional_http_timeout_seconds),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            caps = get_capabilities(client, provider=name)
    except DomainError as exc:
        return {**status, "error_code": exc.code}
    return {
        **status,
        "available": True,
        "status": "ready",
        "version": caps["provider_version"],
        "operations": caps["operations"],
        "error_code": None,
    }


def http_ledger(progress):
    """Project only saved HTTP attempt identities and counts, without request contents."""
    ledger = []
    for operation_id, state in progress.get("professional_operations", {}).items():
        binding = state.get("binding") or {}
        for index, exchange in enumerate(state.get("exchanges", [])):
            saved_response = exchange.get("response")
            response = saved_response if isinstance(saved_response, dict) else {}
            attempts = exchange.get("attempts", 0)
            observed = sorted(
                {
                    value
                    for value in exchange.get("http_attempted_indices", [])
                    if type(value) is int and 0 < value <= attempts
                }
            )
            response_received = (
                isinstance(response, dict)
                and response.get("schema_version") == "bioradar.professional.v1"
                and response.get("provider") in {"biocheck", "mech_evidence"}
                and isinstance(response.get("provider_version"), str)
                and bool(response["provider_version"])
                and (
                    response.get("operation_id") == operation_id
                    and response.get("status") in {"model_required", "completed", "error", "not_applicable"}
                    or state.get("operation") == "capabilities"
                    and isinstance(response.get("operations"), list)
                )
            )
            legacy_received = (
                not observed
                and response_received
                and exchange.get("status") == "received"
                and type(attempts) is int
                and attempts > 0
            )
            known = len(observed) if observed else int(legacy_received)
            ledger.append(
                {
                    "operation_id": operation_id,
                    "provider": binding.get("provider") or state.get("provider") or response.get("provider"),
                    "operation": binding.get("operation") or state.get("operation"),
                    "action_id": state.get("action_id"),
                    "exchange_index": index,
                    "http_attempted_indices": observed,
                    "attempts": attempts,
                    "outcome": exchange.get("status"),
                    "known_tool_calls": known,
                    "reserved_tool_calls": attempts - known,
                    "response_received": response_received,
                    "accounting_basis": "http_attempted_indices"
                    if observed
                    else "legacy_received_response"
                    if legacy_received
                    else "unmeasured_attempts",
                }
            )
    return ledger


def _sync_http_usage(progress):
    """Count attempted requests once; an interrupted send intent remains reserved."""
    known = reserved = 0
    for state in progress.get("professional_operations", {}).values():
        state_known = state_reserved = 0
        for exchange in state.get("exchanges", []):
            attempts = exchange.get("attempts", 0)
            observed = {
                index
                for index in exchange.get("http_attempted_indices", [])
                if type(index) is int and 0 < index <= attempts
            }
            if attempts and exchange.get("status") in {"received", "failed", "unknown"}:
                observed.add(attempts)
            exchange["http_attempted_indices"] = sorted(observed)
            state_known += len(observed)
            state_reserved += attempts - len(observed)
        state["actual_cost"] = {
            "tool_calls": None if state_reserved else state_known,
            "known_tool_calls": state_known,
            "reserved_tool_calls": state_reserved,
        }
        known += state_known
        reserved += state_reserved
    usage = progress.setdefault("tool_usage", {})
    previous = usage.get("professional_tool_calls", 0)
    usage["tool_calls"] = max(0, usage.get("tool_calls", 0) - previous) + known
    usage["professional_tool_calls"] = known
    usage["professional_reserved_tool_calls"] = reserved


def _http_budget_guard(fixed, progress, *, own_reservation=0):
    _sync_http_usage(progress)
    usage = progress["tool_usage"]
    other_reserved = sum(
        (query.get("cost") or {}).get("reserved_tool_calls", 1)
        for query in progress.get("queries", [])
        if query.get("provider") != "professional"
        and query.get("status") in {"sent", "unconfirmed"}
        and (query.get("cost") or {}).get("tool_calls") is None
    )
    remaining = (
        fixed["budget"].get("max_tool_calls", 64)
        - usage["tool_calls"]
        - usage["professional_reserved_tool_calls"]
        - other_reserved
        + own_reservation
    )
    if remaining < 1:
        raise DomainError("analysis_tool_budget", "专业HTTP请求达到统一来源请求预算，未发送新请求", 422)


def load_provider(service, identifier, token, fixed, progress, name):
    """Each operation retains its input before a remote method can request a model.

    Local is explicit compatibility mode. An HTTP fault never changes transport.
    Received operation/model results remain replayable after the time budget ends.
    """
    from datetime import UTC, datetime

    from bioradar.adapters.biocheck import load_biocheck, load_biocheck_http
    from bioradar.adapters.mech_evidence import load_mech_evidence, load_mech_evidence_http
    from bioradar.adapters.professional_rpc import execute_professional, get_capabilities
    from bioradar.research.investigations.analysis import analysis_budget

    mode, url, credential = _configuration(service.settings, name)
    frozen = fixed.get("providers", {}).get(name, {})
    if frozen.get("transport", "local") != mode:
        raise DomainError("analysis_provider_changed", "专业服务执行方式与本轮固定输入不同", 409)
    if mode == "local":
        return load_biocheck(service.settings) if name == "biocheck" else load_mech_evidence(service.settings)
    if not url or not credential or frozen.get("service_endpoint") != url:
        raise DomainError("professional_rpc_not_configured", "本轮固定专业服务当前未配置", 503)
    headers = {"Authorization": "Bearer " + credential.get_secret_value()}
    version = progress.get("provider_versions", {}).get(name)
    if version is None:
        capability_id = canonical_hash([identifier, name, "capabilities"])
        capability = progress.setdefault("professional_operations", {}).setdefault(
            capability_id,
            {"operation": "capabilities", "action_id": progress.get("active_professional_action")},
        )
        exchanges = capability.setdefault("exchanges", [])
        if not exchanges:
            exchanges.append(
                {
                    "request": {"method": "GET", "path": "/v1/capabilities"},
                    "status": "prepared",
                    "attempts": 0,
                }
            )
        exchange = exchanges[-1]
        caps = exchange.get("response")
        if caps is None:
            if exchange.get("status") != "prepared":
                raise DomainError(
                    exchange.get("error_code", "professional_rpc_outcome_unconfirmed"),
                    "专业能力请求已保存停止或待核状态，未自动重发",
                    409,
                )
            service._guard(identifier, token, check_deadline=True)
            _http_budget_guard(fixed, progress)
            exchange.update(status="sent", attempts=1)
            _sync_http_usage(progress)
            service._checkpoint(identifier, token, progress, fixed=fixed)
            try:
                service._guard(identifier, token, check_deadline=True)
                _http_budget_guard(fixed, progress, own_reservation=1)
                remaining = analysis_budget(fixed, progress, datetime.now(UTC))["usage"]["remaining_seconds"]
                if remaining <= 0:
                    raise DomainError("analysis_time_budget", "专业能力请求发送前时限已结束", 409)
            except DomainError as exc:
                exchange.update(status="not_sent", attempts=0, error_code=exc.code)
                _sync_http_usage(progress)
                service._checkpoint(identifier, token, progress, fixed=fixed)
                raise
            try:
                with httpx.Client(
                    base_url=url,
                    headers=headers,
                    timeout=min(service.settings.professional_http_timeout_seconds, remaining),
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    caps = get_capabilities(client, provider=name)
            except DomainError as exc:
                exchange.update(status="failed", error_code=exc.code)
                _sync_http_usage(progress)
                service._checkpoint(identifier, token, progress, fixed=fixed)
                raise
            exchange.update(status="received", response=caps)
            _sync_http_usage(progress)
            service._checkpoint(identifier, token, progress, fixed=fixed)
        version = caps["provider_version"]
        progress.setdefault("professional_capabilities", {})[name] = caps
    service._use_provider(identifier, token, fixed, progress, name, version)
    counters = {}

    def execute(operation, arguments, call=None):
        # An operation's place in the saved workflow is its identity. Changing its
        # arguments after a crash fails rather than silently paying for a new step.
        phase = (progress.get("report_workflow") or {}).get("phase", "legacy")
        round_index = progress.get("active_report_round", 0)
        namespace = (round_index, phase, progress.get("active_professional_action"), operation)
        ordinal = counters.get(namespace, 0)
        counters[namespace] = ordinal + 1
        operation_id = canonical_hash([identifier, name, *namespace, ordinal])
        state = progress.setdefault("professional_operations", {}).setdefault(operation_id, {})
        state.setdefault("action_id", progress.get("active_professional_action"))
        initial_attempts = {
            id(exchange): exchange.get("attempts", 0) for exchange in state.get("exchanges", [])
        }
        active_intent = None

        def checkpoint():
            nonlocal active_intent
            exchanges = state.get("exchanges", [])
            if exchanges:
                exchange = exchanges[-1]
                count = exchange.get("attempts", 0)
                previous = initial_attempts.get(id(exchange), 0)
                if exchange.get("status") == "sent" and count > previous:
                    active_intent = (id(exchange), count)
                elif exchange.get("status") != "sent":
                    active_intent = None
                initial_attempts[id(exchange)] = count
            _sync_http_usage(progress)
            service._checkpoint(identifier, token, progress, fixed=fixed)

        caps = progress.get("professional_capabilities", {}).get(name)
        if caps and operation not in caps["operations"]:
            raise DomainError("professional_operation_unavailable", "专业服务未声明此项操作", 422)
        service._guard(identifier, token)
        with httpx.Client(
            base_url=url,
            headers=headers,
            timeout=service.settings.professional_http_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        ) as client:

            def guard():
                service._guard(identifier, token, check_deadline=True)
                exchange = state.get("exchanges", [None])[-1] if state.get("exchanges") else None
                own = int(
                    exchange is not None
                    and active_intent == (id(exchange), exchange.get("attempts"))
                    and exchange.get("status") == "sent"
                    and exchange.get("attempts") not in exchange.get("http_attempted_indices", [])
                )
                _http_budget_guard(fixed, progress, own_reservation=own)
                remaining = analysis_budget(fixed, progress, datetime.now(UTC))["usage"]["remaining_seconds"]
                if remaining <= 0:
                    raise DomainError("analysis_time_budget", "专业服务发送前研究时限已结束", 409)
                client.timeout = httpx.Timeout(
                    min(service.settings.professional_http_timeout_seconds, remaining),
                    connect=min(5, remaining),
                )

            def model_call(stage, instruction, payload, schema=None, *, call_id):
                if call is None:
                    raise DomainError(
                        "professional_rpc_unexpected_model_request", "本操作不允许模型请求", 409
                    )
                return call(
                    stage,
                    instruction,
                    payload,
                    schema,
                    call_id=name + ":" + operation_id + ":" + call_id,
                )

            result = execute_professional(
                client,
                provider=name,
                operation_id=operation_id,
                operation=operation,
                arguments=arguments,
                call=model_call if call else None,
                state=state,
                checkpoint=checkpoint,
                provider_version=version,
                guard=guard,
                max_model_calls=service.settings.professional_max_steps,
                max_request_bytes=service.settings.professional_http_max_bytes,
            )
        service._use_provider(identifier, token, fixed, progress, name, result["provider_version"])
        return result

    if name == "biocheck":
        return load_biocheck_http(execute, version)
    refs = []
    for artifact in progress.get("kdd_artifacts", []):
        if artifact.get("kind") == "pattern_result":
            refs.append(
                {
                    "run_id": artifact["id"],
                    "result_id": artifact["id"],
                    "version": artifact["version"],
                    "finding_ids": artifact.get("candidate_keys", []),
                }
            )
    return load_mech_evidence_http(execute, version, context={"computation_refs": refs})


def _completed_mechanism_rpc(identifier, fixed, progress, packet):
    """Only this operation's confirmed response can bypass a new-work reservation."""
    import hashlib

    from bioradar.adapters.mech_evidence import _case
    from bioradar.adapters.professional_rpc import SCHEMA_VERSION, _canonical

    frozen = fixed.get("providers", {}).get("mech_evidence", {})
    version = progress.get("provider_versions", {}).get("mech_evidence") or frozen.get("version")
    if frozen.get("transport", "local") != "http" or not version:
        return False
    phase = (progress.get("report_workflow") or {}).get("phase", "legacy")
    operation_id = canonical_hash(
        [
            identifier,
            "mech_evidence",
            progress.get("active_report_round", 0),
            phase,
            progress.get("active_professional_action"),
            "evaluate",
            0,
        ]
    )
    state = progress.get("professional_operations", {}).get(operation_id, {})
    # A completed exchange may be durable before the derived operation status is saved.
    # Its fixed binding and response remain subject to execute_professional's validator.
    if state.get("provider_version") != version:
        return False
    case, _, _ = _case(packet)
    arguments = {
        "mode": "biomedical_research",
        "task": "mechanism",
        "case": case,
        "computation_refs": [
            {
                "run_id": artifact["id"],
                "result_id": artifact["id"],
                "version": artifact["version"],
                "finding_ids": artifact.get("candidate_keys", []),
            }
            for artifact in progress.get("kdd_artifacts", [])
            if artifact.get("kind") == "pattern_result"
        ],
    }
    binding = {
        "schema_version": SCHEMA_VERSION,
        "provider": "mech_evidence",
        "operation_id": operation_id,
        "operation": "evaluate",
        "arguments_hash": hashlib.sha256(_canonical(arguments)).hexdigest(),
    }
    exchanges = state.get("exchanges", [])
    return (
        state.get("binding") == binding
        and bool(exchanges)
        and (exchanges[-1].get("response") or {}).get("status") == "completed"
    )


def mechanism(service, identifier, token, fixed, progress, reading):
    from bioradar.adapters.biocheck import build_packet
    from bioradar.adapters.mech_evidence import evaluate_mechanism

    if "mechanism_evaluation" not in progress:
        service._guard(identifier, token)
        progress["stage"] = "mechanism_evaluation"
        service._checkpoint(identifier, token, progress)
        packet = build_packet(
            fixed["question"],
            progress["evidence"],
            {
                "mechanism": reading.mechanism_candidate,
                "qualification": "machine_proposed_candidate_not_source_fact",
                **(
                    {
                        "research_contract_version": "bioradar-research-input-v1",
                        "research_scope": {"evidence_time": copy.deepcopy(fixed["evidence_time"])},
                    }
                    if fixed.get("query_policy", {}).get("research_contract_version")
                    == "bioradar-research-input-v1" else {}
                ),
            },
        )
        if not reading.mechanism_candidate or not fixed.get("providers", {}).get("mech_evidence", {}).get(
            "available"
        ):
            assessment = evaluate_mechanism(
                service.settings,
                packet,
                service._shared_callback(identifier, token, fixed, progress),
                loaded=(None, None, None),
            )
        elif not _completed_mechanism_rpc(
            identifier, fixed, progress, packet
        ) and not service._optional_budget(fixed, progress, 3, following=1):
            assessment = {
                "provider": "mech_evidence",
                "status": "skipped_budget",
                "error_code": "analysis_model_budget",
            }
        else:
            try:
                loaded = service._load_professional(identifier, token, fixed, progress, "mech_evidence")
                service._use_provider(identifier, token, fixed, progress, "mech_evidence", loaded[-1])
                assessment = evaluate_mechanism(
                    service.settings,
                    packet,
                    service._shared_callback(identifier, token, fixed, progress),
                    loaded=loaded,
                )
            except DomainError as exc:
                if exc.code not in {"analysis_model_budget", "analysis_token_budget"}:
                    raise
                assessment = {
                    "provider": "mech_evidence",
                    "status": "skipped_budget",
                    "error_code": exc.code,
                }

        progress["mechanism_evaluation"] = assessment
        service._checkpoint(identifier, token, progress)
    assessment = progress["mechanism_evaluation"]
    if (
        fixed.get("query_context_version") == "semantic-v2"
        and fixed.get("query_policy", {}).get("effective") == "query_planner"
        and assessment.get("status") == "completed"
        and "passage_bindings" not in assessment
    ):
        from bioradar.adapters.mech_evidence import mechanism_passage_bindings

        local = next(
            (c for c in progress["calls"] if c["stage"] == "mech_local_review" and c["status"] == "received"),
            None,
        )
        if local is not None:
            packet = build_packet(fixed["question"], progress["evidence"])
            assessment.update(
                mechanism_passage_bindings(
                    packet,
                    local["payload"]["materials"],
                    assessment=assessment,
                    radar_evidence=progress["evidence"],
                )
            )
    fixed["mechanism_evaluation"] = progress["mechanism_evaluation"]
    service._checkpoint(identifier, token, progress, fixed=fixed)
    return progress
