"""HTTP API used by the Vue app and by scripts. Interactive docs at /docs."""

import hmac
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from bioradar import db
from bioradar.config import settings
from bioradar.research import analyses, monitor

app = FastAPI(title="BioRadar", version="1.0.0")
COOKIE = "bioradar_session"


@app.exception_handler(analyses.NotFound)
def not_found(_, exc):
    return JSONResponse({"error_code": "not_found", "message": str(exc)}, status_code=404)


@app.exception_handler(analyses.InvalidInput)
def bad_input(_, exc):
    return JSONResponse({"error_code": "invalid_input", "message": str(exc)}, status_code=422)


def signed_in(request: Request):
    value = request.cookies.get(COOKIE) or request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not value or not hmac.compare_digest(value, settings().dev_credential):
        raise HTTPException(401, "请先登录")


@app.get("/health")
def health():
    with db.session() as s:
        s.execute(text("select 1"))
    return {"status": "ok"}


class Login(BaseModel):
    credential: str


@app.post("/api/session")
def login(body: Login, response: Response):
    if not hmac.compare_digest(body.credential, settings().dev_credential):
        raise HTTPException(401, "登录凭据不正确")
    response.set_cookie(COOKIE, body.credential, httponly=True, samesite="lax")
    return {"signed_in": True}


@app.delete("/api/session")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"signed_in": False}


@app.get("/api/session")
def session_state(request: Request):
    try:
        signed_in(request)
    except HTTPException:
        return {"signed_in": False}
    return {"signed_in": True}


api = [Depends(signed_in)]


@app.get("/api/options", dependencies=api)
def options():
    s = settings()
    return {
        "sources": s.sources,
        "demo_sources": s.demo_sources,
        "directions": sorted(analyses.DIRECTIONS),
        "methods": ["planner", "baseline"],
        "configured": {
            "model": bool(s.llm_base_url and s.llm_model),
            "planner": bool(s.planner_url),
            "biocheck": bool(s.biocheck_url),
            "mechevidence": bool(s.mechevidence_url),
            "weknora": bool(s.weknora_url),
        },
        "limits": {"max_steps": s.max_steps, "max_reads": s.max_reads, "max_llm_calls": s.max_llm_calls},
        "monitor": {"enabled": s.monitor_enabled, "auto_analysis": s.auto_analysis},
    }


class Ask(BaseModel):
    question: str = Field(min_length=4, max_length=2000)
    direction: Literal["pipeline", "science", "business", "public_health"]
    method: Literal["planner", "baseline"] = "planner"
    is_demo: bool = False
    sources: list[str] | None = None
    cutoff: datetime | None = Field(default=None, description="历史回放的截止时间，必须带时区")
    scope_record_ids: list[str] = Field(default=[], description="只在这些记录里检索和阅读（固定材料集）")


@app.post("/api/analyses", dependencies=api)
def ask(body: Ask):
    row = analyses.create(
        question=body.question,
        direction=body.direction,
        method=body.method,
        is_demo=body.is_demo,
        sources=body.sources,
        cutoff=body.cutoff.isoformat() if body.cutoff else None,
        scope_record_ids=body.scope_record_ids,
    )
    return analyses.view(row)


class Followup(BaseModel):
    question: str = Field(min_length=4, max_length=2000)


@app.post("/api/analyses/{analysis_id}/followup", dependencies=api)
def followup(analysis_id: str, body: Followup):
    parent = analyses.get(analysis_id)
    row = analyses.create(
        question=body.question,
        direction=parent.input["direction"],
        method=parent.method,
        is_demo=parent.input["is_demo"],
        sources=parent.input["sources"],
        cutoff=parent.input.get("cutoff"),
        scope_record_ids=parent.input.get("scope_record_ids", ()),
        investigation_id=parent.investigation_id,
        parent_id=parent.id,
        trigger="followup",
    )
    return analyses.view(row)


@app.get("/api/analyses", dependencies=api)
def list_analyses(investigation_id: str | None = None, limit: int = 30):
    with db.session() as s:
        query = select(db.Analysis).order_by(db.Analysis.created_at.desc()).limit(min(limit, 100))
        if investigation_id:
            query = query.where(db.Analysis.investigation_id == investigation_id)
        rows = list(s.scalars(query))
    return [analyses.view(r) for r in rows]


@app.get("/api/analyses/{analysis_id}", dependencies=api)
def get_analysis(analysis_id: str):
    return analyses.view(analyses.get(analysis_id), full=True)


@app.get("/api/analyses/{analysis_id}/evidence/{ref}", dependencies=api)
def evidence(analysis_id: str, ref: str):
    return analyses.evidence_text(analysis_id, ref)


@app.get("/api/analyses/{analysis_id}/export", dependencies=api, response_class=PlainTextResponse)
def export(analysis_id: str):
    value = analyses.view(analyses.get(analysis_id), full=True)
    result = value["result"] or {}
    lines = [
        f"# {value['question']}",
        "",
        f"- 方法：{value['method']}　状态：{value['state']}　核验：{result.get('report_status', 'NOT_RUN')}",
        f"- 截止时间：{value['cutoff'] or '当前'}　模型调用：{result.get('usage', {}).get('llm_calls', 0)}",
        "",
        result.get("draft") or "（没有生成报告）",
        "",
        "## 材料",
        "",
    ]
    for e in value["evidence"]:
        lines.append(
            f"- [{e['ref']}] {e['title']}（{e['source']} {e['record_id']}，版本 {e['version_id'][:12]}，"
            f"{e['published_at'] or '日期未知'}{'，未核实' if not e['verified'] else ''}）"
        )
    assessment = result.get("assessment") or {}
    if assessment.get("judgments"):
        lines += ["", "## 判断", ""]
        for j in assessment["judgments"]:
            lines.append(
                f"- {j['question']}：{j['conclusion']}（置信 {j['confidence']}，真实性 {j['credibility']}，"
                f"后果 {j['consequence']}，优先级 {j['priority']}）待证实：{j['unproven']}"
            )
    return "\n".join(lines)


@app.get("/api/investigations", dependencies=api)
def investigations(limit: int = 30):
    with db.session() as s:
        rows = list(
            s.scalars(select(db.Investigation).order_by(db.Investigation.created_at.desc()).limit(limit))
        )
        counts = dict(
            s.execute(
                select(db.Analysis.investigation_id, func.count()).group_by(db.Analysis.investigation_id)
            ).all()
        )
    return [
        {
            "id": r.id,
            "question": r.question,
            "direction": r.direction,
            "is_demo": r.is_demo,
            "topic_id": r.topic_id,
            "created_at": r.created_at.isoformat(),
            "analyses": counts.get(r.id, 0),
        }
        for r in rows
    ]


@app.get("/api/topics", dependencies=api)
def topics(is_demo: bool = False):
    with db.session() as s:
        rows = list(
            s.scalars(
                select(db.Topic).where(db.Topic.is_demo == is_demo).order_by(db.Topic.last_activity_at.desc())
            )
        )
    return [monitor.topic_view(t) for t in rows]


@app.get("/api/topics/{topic_id}", dependencies=api)
def topic(topic_id: str):
    with db.session() as s:
        row = s.get(db.Topic, topic_id)
    if row is None:
        raise analyses.NotFound(topic_id)
    return monitor.topic_view(row, events=True)


class Tick(BaseModel):
    is_demo: bool = False
    cutoff: datetime | None = None
    clock: datetime | None = None


@app.post("/api/monitor/tick", dependencies=api)
def tick(body: Tick):
    """Run one monitoring round now. Simulations and replays pass a source clock or cutoff."""
    return monitor.tick(is_demo=body.is_demo, cutoff=body.cutoff, clock=body.clock)


@app.get("/api/monitor/observations", dependencies=api)
def observations(is_demo: bool = False, limit: int = 50):
    with db.session() as s:
        rows = list(
            s.scalars(
                select(db.Observation)
                .where(db.Observation.is_demo == is_demo)
                .order_by(db.Observation.available_at.desc())
                .limit(min(limit, 200))
            )
        )
    return [
        {
            "title": o.title,
            "source": o.source,
            "record_id": o.record_id,
            "topic_id": o.topic_id,
            "available_at": o.available_at.isoformat(),
            "alert": o.alert,
            "closure": o.closure,
            "reason": o.reason,
        }
        for o in rows
    ]


@app.get("/api/usage", dependencies=api)
def usage():
    with db.session() as s:
        rows = s.execute(
            select(
                func.date(db.LlmCall.created_at),
                db.LlmCall.service,
                func.count(),
                func.sum(db.LlmCall.total_tokens),
            )
            .group_by(func.date(db.LlmCall.created_at), db.LlmCall.service)
            .order_by(func.date(db.LlmCall.created_at).desc())
            .limit(90)
        ).all()
    return [
        {"day": str(day), "service": service, "calls": calls, "tokens": int(tokens or 0)}
        for day, service, calls, tokens in rows
    ]
