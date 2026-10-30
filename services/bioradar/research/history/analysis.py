"""Run a saved historical question through the ordinary investigation executor."""

import copy
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from bioradar.api.errors import DomainError
from bioradar.research.investigations.analysis_models import AnalysisCreate
from bioradar.research.investigations.analysis_service import AnalysisService, InvestigationAnalysisRecord
from bioradar.research.investigations.models import InvestigationEntryCreate
from bioradar.research.investigations.service import InvestigationService
from bioradar.research.mining.models import canonical_hash


def _remember(history, actor, study_id, key, field, value):
    with Session(history.engine.database) as session, session.begin():
        row = history._row(session, actor, study_id, lock=True)
        payload = copy.deepcopy(row.payload)
        intent = payload["semantic_requests"][key]
        saved = copy.deepcopy(intent.setdefault(field, value))
        row.payload = payload
    return saved


def semantic_summaries(session, row):
    entries = sorted(
        row.payload.get("semantic_requests", {}).values(),
        key=lambda value: value.get("created_at", ""),
        reverse=True,
    )
    summaries = []
    for entry in entries[:20]:
        identifier = entry.get("analysis_id")
        analysis = session.get(InvestigationAnalysisRecord, identifier) if identifier else None
        if analysis is None:
            continue
        summaries.append(
            {
                "id": analysis.id,
                "investigation_id": analysis.investigation_id,
                "state": analysis.state,
                "created_at": analysis.created_at.isoformat(),
                "query_policy": analysis.fixed_input.get("query_policy"),
                "evidence_time": analysis.fixed_input.get("evidence_time"),
            }
        )
    return summaries


def start_history_analysis(history, actor, study_id, request, idempotency_key):
    """Freeze the study's feature scope before starting the shared RQ path.

    Predictions, later outcome records and evaluation scores are never model input.
    The intent keeps one stable start body if creation commits before HTTP returns.
    """
    study = history.get(actor, study_id)
    if not study.investigation_id or study.state in {"collecting", "failed"}:
        raise DomainError("history_analysis_unavailable", "历史资料尚未采用完成，暂不能启动调查", 409)
    requested = request.model_dump(mode="json")
    key = "history-analysis:" + canonical_hash([study_id, idempotency_key])
    with Session(history.engine.database) as session, session.begin():
        row = history._row(session, actor, study_id, lock=True)
        payload = copy.deepcopy(row.payload)
        intents = payload.setdefault("semantic_requests", {})
        intent = intents.get(key)
        if intent is not None and intent["request_hash"] != canonical_hash(requested):
            raise DomainError("idempotency_conflict", "同一请求编号不能更换历史调查条件", 409)
        if intent is None:
            intent = {
                "request_hash": canonical_hash(requested),
                "request": requested,
                "created_at": datetime.now(UTC).isoformat(),
            }
            intents[key] = intent
            row.payload = payload
        intent = copy.deepcopy(intent)
        feature_snapshots = {
            item["snapshot_id"]
            for item in payload.get("snapshots", [])
            if item.get("purpose") == "features"
            and datetime.fromisoformat(item["cutoff"]) <= study.cutoff
        }

    analyses = AnalysisService(history.engine, history.reader)
    if intent.get("analysis_id"):
        return analyses.get(actor, study.investigation_id, intent["analysis_id"])
    investigations = InvestigationService(history.engine)
    view = investigations.get(actor, study.investigation_id)
    if view.status == "closed":
        raise DomainError("investigation_closed", "该历史调查已关闭，请先明确重新打开调查", 409)
    if view.status == "paused" or intent.get("open_body"):
        body = intent.get("open_body") or {
            "expected_revision": view.revision,
            "kind": "status",
            "status": "open",
            "text": "按历史研究已冻结的资料时点启动调查；后来结局不进入本轮输入。",
        }
        body = _remember(history, actor, study_id, key, "open_body", body)
        investigations.append(
            actor,
            study.investigation_id,
            InvestigationEntryCreate.model_validate(body),
            key + ":open",
        )
        view = investigations.get(actor, study.investigation_id)

    selected = [
        entry.id
        for entry in view.entries
        if entry.kind == "material" and entry.payload.snapshot_id in feature_snapshots
    ]
    if not selected and any(entry.kind == "material" for entry in view.entries):
        raise DomainError(
            "history_analysis_basis_invalid", "调查中的材料不属于本研究的历史特征版本，未发送模型", 409
        )
    body = intent.get("start_body") or {
        "expected_revision": view.revision,
        "question": study.request.question,
        "auto_retrieve": True,
        "source_ids": [study.source_id],
        "material_entry_ids": selected[-10:],
        "query_policy": request.query_policy,
        "budget": request.budget.model_dump(mode="json") if request.budget else None,
    }
    body = _remember(history, actor, study_id, key, "start_body", body)
    _, accepted = analyses.start(
        actor,
        study.investigation_id,
        AnalysisCreate.model_validate(body),
        key,
        historical_context={
            "study_id": study.id,
            "mode": study.request.mode,
            "cutoff": study.cutoff.isoformat(),
            "run_id": study.request.run_id,
        },
    )
    _remember(history, actor, study_id, key, "analysis_id", accepted["id"])
    return analyses.get(actor, study.investigation_id, accepted["id"])
