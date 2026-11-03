"""HTTP API used by the Vue app and by scripts. Interactive docs at /docs."""

import copy
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from bioradar import db
from bioradar.adapters.fabric import Fabric, FabricError
from bioradar.config import settings
from bioradar.research import analyses, monitor

app = FastAPI(title="BioRadar", version="1.0.0")
COOKIE = "bioradar_session"
SESSION_SECONDS = 12 * 3600
ROLES = {"viewer": 0, "analyst": 1, "admin": 2}  # viewer reads; analyst asks and reviews; admin manages users


@app.exception_handler(analyses.NotFound)
def not_found(_, exc):
    return JSONResponse({"error_code": "not_found", "message": str(exc)}, status_code=404)


@app.exception_handler(analyses.InvalidInput)
def bad_input(_, exc):
    return JSONResponse({"error_code": "invalid_input", "message": str(exc)}, status_code=422)


def hash_credential(credential: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(credential.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def credential_ok(credential: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    given = hashlib.scrypt(credential.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(given.hex(), digest)


def _sign(body: str) -> str:
    """Signed with BIORADAR_SESSION_SECRET only, never with a login credential (those change hands)."""
    key = settings().session_secret
    return hmac.new(key.encode(), body.encode(), "sha256").hexdigest() if key else ""


def session_token(user: db.User) -> str:
    body = f"{user.id}.{user.session_version}.{int(time.time()) + SESSION_SECONDS}"
    return f"{body}.{_sign(body)}"


def current_user(request: Request) -> db.User | None:
    """The signed-in person, read again from the table on every request: a role change takes effect at
    once, a new credential or deactivation ends every older session (session_version).
    The token is the cookie, or the same value as a Bearer header."""
    value = request.cookies.get(COOKIE) or request.headers.get("Authorization", "").removeprefix("Bearer ")
    try:
        user_id, version, expires, signature = value.split(".")
        expired = int(expires) < time.time()
    except ValueError:
        return None
    body = f"{user_id}.{version}.{expires}"
    if expired or not signature or not hmac.compare_digest(signature, _sign(body) or "-"):
        return None
    with db.session() as s:
        user = s.get(db.User, user_id)
    if user is None or not user.active or str(user.session_version) != version:
        return None
    return user


def require(role: str):
    def check(request: Request) -> db.User:
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "请先登录")
        if ROLES[user.role] < ROLES[role]:
            raise HTTPException(403, "当前账号没有这项权限")
        return user

    return check


@app.get("/health")
def health():
    with db.session() as s:
        s.execute(text("select 1"))
    return {"status": "ok"}


class Login(BaseModel):
    name: str = "admin"
    credential: str


@app.post("/api/session")
def login(body: Login, response: Response):
    """The first sign-in with BIORADAR_DEV_CREDENTIAL as "admin", while there are no users yet, creates
    that admin. From then on only the users table counts."""
    if not settings().session_secret:
        raise HTTPException(503, "BIORADAR_SESSION_SECRET 未配置，不能登录")
    with db.session() as s:
        user = s.scalar(select(db.User).where(db.User.name == body.name))
        dev = settings().dev_credential
        if (
            user is None
            and body.name == "admin"
            and dev
            and hmac.compare_digest(body.credential, dev)
            and not s.scalar(select(func.count()).select_from(db.User))
        ):
            user = db.User(name="admin", role="admin", credential_hash=hash_credential(dev))
            s.add(user)
            s.flush()
        if user is None or not user.active or not credential_ok(body.credential, user.credential_hash):
            raise HTTPException(401, "用户名或登录凭据不正确")
    response.set_cookie(COOKIE, session_token(user), httponly=True, samesite="lax", max_age=SESSION_SECONDS)
    return {"signed_in": True, "name": user.name, "role": user.role}


@app.delete("/api/session")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"signed_in": False}


@app.get("/api/session")
def session_state(request: Request):
    user = current_user(request)
    return {"signed_in": True, "name": user.name, "role": user.role} if user else {"signed_in": False}


api = [Depends(require("viewer"))]
analyst = [Depends(require("analyst"))]
admin = [Depends(require("admin"))]


class NewUser(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[\w.-]+$")
    role: Literal["viewer", "analyst", "admin"]
    credential: str = Field(min_length=8, max_length=200)


class UserChange(BaseModel):
    role: Literal["viewer", "analyst", "admin"] | None = None
    credential: str | None = Field(default=None, min_length=8, max_length=200)
    active: bool | None = Field(default=None, description="false 停用：立即结束他所有的会话")


def user_view(u: db.User) -> dict:
    return {"name": u.name, "role": u.role, "active": u.active, "created_at": u.created_at.isoformat()}


@app.get("/api/users", dependencies=admin)
def users():
    with db.session() as s:
        return [user_view(u) for u in s.scalars(select(db.User).order_by(db.User.created_at))]


@app.post("/api/users", dependencies=admin)
def add_user(body: NewUser):
    with db.session() as s:
        if s.scalar(select(db.User).where(db.User.name == body.name)):
            raise HTTPException(409, "用户名已存在")
        user = db.User(name=body.name, role=body.role, credential_hash=hash_credential(body.credential))
        s.add(user)
        s.flush()
        return user_view(user)


@app.patch("/api/users/{name}", dependencies=admin)
def change_user(name: str, body: UserChange):
    """Change a role, replace a credential (also how the first admin stops using the dev credential) or
    deactivate someone. A new credential or deactivation ends all of that person's sessions."""
    with db.session() as s:
        user = s.scalar(select(db.User).where(db.User.name == name))
        if user is None:
            raise analyses.NotFound(name)
        losing_admin = user.role == "admin" and user.active and (
            (body.role and body.role != "admin") or body.active is False
        )
        if losing_admin:
            admins = s.scalar(
                select(func.count()).select_from(db.User).where(db.User.role == "admin", db.User.active)
            )
            if admins <= 1:
                raise HTTPException(409, "至少保留一个启用的管理员")
        if body.role:
            user.role = body.role
        if body.credential:
            user.credential_hash = hash_credential(body.credential)
            user.session_version += 1
        if body.active is not None and body.active != user.active:
            user.active = body.active
            user.session_version += 1
        return user_view(user)


# ---- source access: Fabric's source list is the authority ----------------------------------------

READABLE_SECONDS = 300
_readable: dict = {"at": None, "sources": None}


def readable_sources() -> set[str] | None:
    """Sources Fabric lets the radar read now. A revoked source drops out of Fabric's list (it is not
    listed as false). Cached five minutes; when the list cannot be read the answer is None, and nothing
    is shown as text until it can be read again."""
    now = time.monotonic()
    if _readable["at"] is None or now - _readable["at"] >= READABLE_SECONDS:
        try:
            listed = Fabric(settings()).sources()["sources"]
            _readable.update(at=now, sources={r["source_id"] for r in listed})
        except (FabricError, httpx.HTTPError, KeyError, TypeError, ValueError):
            _readable.update(at=None, sources=None)
    return _readable["sources"]


def withheld(source: str, readable: set[str] | None) -> str | None:
    if readable is None:
        return "来源授权状态暂时无法确认"
    return None if source in readable else "来源已撤权"


# Enum-like fields kept when model-written text is withheld; every other string is blanked.
KEEP = {
    "id", "ref", "index", "check", "status", "verdict", "confidence", "credibility", "consequence",
    "priority", "alert", "level", "stance", "kind", "question", "grounds", "citations", "found",
}


def blank(value):
    """Model-written text derived from the materials, with the words removed and the structure kept."""
    if isinstance(value, dict):
        return {k: (v if k in KEEP else blank(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [blank(v) for v in value]
    return "" if isinstance(value, str) else value


def hidden_refs(rows, readable) -> dict:
    """{ref: reason} for the materials whose source cannot be read now."""
    reasons = {r["ref"]: withheld(r["source"], readable) for r in rows}
    return {ref: reason for ref, reason in reasons.items() if reason}


def notice(hidden: dict) -> str:
    listed = "、".join(f"{ref}（{reason}）" for ref, reason in sorted(hidden.items()))
    return f"引用的材料 {listed} 现在不可读：报告正文、逐句核验、判断与机制评价的文字暂不显示，编号和状态保留"


def redact(value: dict) -> dict:
    """The one output check behind the analysis exits (original text, detail, list, export). When any
    material of an analysis comes from a source that cannot be read now, its own entry keeps only number,
    source and record id, and every piece of model-written text that could carry its words (the draft,
    sentence checks, judgments and hypotheses, the mechanism evaluation) is withheld with the reason;
    numbers, states and references stay. Nothing stored is changed."""
    readable = readable_sources()
    hidden = hidden_refs(value["evidence"], readable)
    if not hidden:
        return value
    value = copy.deepcopy(value)
    for e in value["evidence"]:
        if e["ref"] in hidden:
            e.update(title="", url=None, withheld=hidden[e["ref"]])
    for f in (value.get("progress") or {}).get("facts") or []:
        if f.get("ref") in hidden:
            f.update(text="", quote="", withheld=hidden[f["ref"]])
    result = value.get("result") or {}
    if result:
        result["withheld"] = notice(hidden)
        result["draft"] = ""
        for key in ("review", "assessment", "mechanism"):
            if result.get(key):
                result[key] = blank(result[key])
        for c in result.get("computations") or []:
            for o in c.get("operands") or []:
                if o.get("ref") in hidden:
                    o["quote"] = ""
    return value


def redact_summaries(values: list[dict]) -> list[dict]:
    """The analysis list carries each analysis's assessment: same rule as the detail."""
    ids = [v["id"] for v in values if (v.get("result") or {}).get("assessment")]
    if not ids:
        return values
    with db.session() as s:
        rows = s.execute(
            select(db.Evidence.analysis_id, db.Evidence.ref, db.Evidence.source).where(
                db.Evidence.analysis_id.in_(ids)
            )
        ).all()
    readable = readable_sources()
    by_analysis = {}
    for analysis_id, ref, source in rows:
        by_analysis.setdefault(analysis_id, []).append({"ref": ref, "source": source})
    for v in values:
        hidden = hidden_refs(by_analysis.get(v["id"], []), readable)
        if hidden:
            assessment = blank(v["result"]["assessment"])
            v["result"] = dict(v["result"], assessment=assessment, withheld=notice(hidden))
    return values


def redact_observation(row: dict, readable) -> dict:
    reason = withheld(row["source"], readable)
    return dict(row, title="", reason="", withheld=reason) if reason else row


def redact_topics(values: list[dict]) -> list[dict]:
    """Topics and their observations copy record titles and triage text: a topic created from a record
    whose source cannot be read now shows no title; such records' titles, reasons and event details
    (titles, object names) are withheld. Record ids, states and numbers stay."""
    readable = readable_sources()
    with db.session() as s:
        created = dict(
            s.execute(
                select(db.Observation.topic_id, db.Observation.source)
                .join(
                    db.TopicEvent,
                    (db.TopicEvent.topic_id == db.Observation.topic_id) & (db.TopicEvent.kind == "created"),
                )
                .where(db.Observation.topic_id.in_([v["id"] for v in values]))
                .where(db.Observation.record_id == db.TopicEvent.detail["record"].as_string())
            ).all()
        )
    out = []
    for v in values:
        v = copy.deepcopy(v)
        reason = withheld(created[v["id"]], readable) if v["id"] in created else None
        if reason or (readable is None):
            v.update(title="（专题标题暂不显示）", summary="", withheld=reason or withheld("", None))
        if "observations" in v:
            sources = {o["record_id"]: o["source"] for o in v["observations"]}
            v["observations"] = [redact_observation(o, readable) for o in v["observations"]]
            for e in v.get("events", []):
                record = e.get("record")
                if record and withheld(sources.get(record, ""), readable):
                    for key in ("title", "objects", "object", "metric", "scope", "unit", "quote"):
                        if key in e:
                            e[key] = [] if isinstance(e[key], list) else ""
                    e["withheld"] = withheld(sources.get(record, ""), readable)
        out.append(v)
    return out


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


@app.post("/api/analyses", dependencies=analyst)
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


@app.post("/api/analyses/{analysis_id}/followup", dependencies=analyst)
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
    return redact_summaries([analyses.view(r) for r in rows])


@app.get("/api/analyses/{analysis_id}", dependencies=api)
def get_analysis(analysis_id: str):
    return redact(analyses.view(analyses.get(analysis_id), full=True))


@app.get("/api/analyses/{analysis_id}/evidence/{ref}", dependencies=api)
def evidence(analysis_id: str, ref: str):
    item = analyses.evidence_text(analysis_id, ref)
    reason = withheld(item["source"], readable_sources())
    if reason:
        keep = ("ref", "source", "record_id", "version_id")
        return {**{k: item[k] for k in keep}, "title": "", "text": "", "url": None, "withheld": reason}
    return item


@app.get("/api/analyses/{analysis_id}/export", dependencies=api, response_class=PlainTextResponse)
def export(analysis_id: str):
    value = redact(analyses.view(analyses.get(analysis_id), full=True))
    result = value["result"] or {}
    lines = [
        f"# {value['question']}",
        "",
        f"- 方法：{value['method']}　状态：{value['state']}　核验：{result.get('report_status', 'NOT_RUN')}",
        f"- 截止时间：{value['cutoff'] or '当前'}　模型调用：{result.get('usage', {}).get('llm_calls', 0)}",
        "",
        result.get("withheld") or result.get("draft") or "（没有生成报告）",
        "",
        "## 材料",
        "",
    ]
    for e in value["evidence"]:
        if e.get("withheld"):
            lines.append(f"- [{e['ref']}] {e['withheld']}（{e['source']} {e['record_id']}）")
            continue
        lines.append(
            f"- [{e['ref']}] {e['title']}（{e['source']} {e['record_id']}，版本 {e['version_id'][:12]}，"
            f"{e['published_at'] or '日期未知'}{'，未核实' if not e['verified'] else ''}）"
        )
    assessment = result.get("assessment") or {}
    if assessment.get("judgments"):
        lines += ["", "## 判断", ""]
        checked = {"supported": "核验通过", "contradicted": "与原文矛盾", "unverified": "未核验通过"}
        for j in assessment["judgments"]:
            lines.append(
                f"- {j['question']}：{j['conclusion']}（{checked.get(j.get('check'), '未核验')}，"
                f"置信 {j['confidence']}，真实性 {j['credibility']}，"
                f"后果 {j['consequence']}，优先级 {j['priority']}）待证实：{j['unproven']}"
            )
    return "\n".join(lines)


class ReviewIn(BaseModel):
    action: Literal["confirm", "reject", "flag_source", "set_alert"]
    analysis_id: str | None = None
    sentence: int | None = Field(default=None, description="confirm / reject：BioCheck 的句子序号")
    evidence_ref: str | None = Field(default=None, description="flag_source：材料编号，如 E3")
    topic_id: str | None = Field(default=None, description="set_alert：专题")
    alert: Literal["normal", "important", "urgent"] | None = None
    note: str = Field(default="", max_length=2000)


@app.post("/api/reviews")
def add_review(body: ReviewIn, user: db.User = Depends(require("analyst"))):
    """A person's decision on a sentence, a material or a topic's alert. Rejecting a sentence or flagging
    a material owes the analysis's topic a new analysis ("analyst_review")."""
    return analyses.review(user, **body.model_dump())


@app.get("/api/reviews", dependencies=api)
def list_reviews(analysis_id: str | None = None, topic_id: str | None = None, limit: int = 50):
    return analyses.reviews(analysis_id=analysis_id, topic_id=topic_id, limit=min(limit, 200))


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
    return redact_topics([monitor.topic_view(t) for t in rows])


@app.get("/api/topics/{topic_id}", dependencies=api)
def topic(topic_id: str):
    with db.session() as s:
        row = s.get(db.Topic, topic_id)
    if row is None:
        raise analyses.NotFound(topic_id)
    return redact_topics([monitor.topic_view(row, events=True)])[0]


class Tick(BaseModel):
    is_demo: bool = False
    cutoff: datetime | None = None
    clock: datetime | None = None


@app.post("/api/monitor/tick", dependencies=analyst)
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
    readable = readable_sources()
    return [
        redact_observation(
            {
                "title": o.title,
                "source": o.source,
                "record_id": o.record_id,
                "topic_id": o.topic_id,
                "available_at": o.available_at.isoformat(),
                "alert": o.alert,
                "closure": o.closure,
                "reason": o.reason,
            },
            readable,
        )
        for o in rows
    ]


def source_row(fabric: Fabric, source: str, cursor, now: datetime, quiet_hours: int) -> dict:
    """Three things kept apart: Fabric's crawl-task status, the radar's last new record, and the
    newest material's publication time. No combined freshness score."""
    last_new = cursor.last_new_at.isoformat() if cursor and cursor.last_new_at else None
    row = {"source": source, "last_new_at": last_new}
    try:
        health = fabric.source_health(source)
        collection = health.get("collection") or {}
        row["collection"] = {
            k: collection.get(k)
            for k in ("state", "last_recorded_success_at", "last_recorded_failure_at", "scope", "coverage")
        }
        row["failed_tasks"] = [
            {k: w.get(k) for k in ("watch_id", "scope", "window_ended_at")}
            for w in health.get("watches") or []
            if w.get("state") == "failed"
        ]
        latest = fabric.latest(source, is_demo=False)
        head = fabric.head(source, is_demo=False)
        row["latest_published_at"] = (latest or {}).get("published_at")
        row["lag"] = head - cursor.after_seq if cursor else None
    except (FabricError, httpx.HTTPError) as exc:
        row["collection"] = {"state": "unavailable", "error": getattr(exc, "code", type(exc).__name__)}
        row["failed_tasks"], row["latest_published_at"], row["lag"] = [], None, None
    last = monitor.parse_time(cursor.last_new_at) if cursor and cursor.last_new_at else None
    quiet = last is None or now - last > timedelta(hours=quiet_hours)
    if row["failed_tasks"]:
        row["note"] = "采集任务失败：" + "、".join(t["watch_id"] or "?" for t in row["failed_tasks"])
    elif quiet and row["collection"].get("state") in {None, "unknown", "unavailable"}:
        row["note"] = "长时间无新增，采集状态待核"
    else:
        row["note"] = ""
    return row


@app.get("/api/sources", dependencies=api)
def source_status():
    """Monitored sources: collection status, last new record, newest publication time; and today's
    automatic token use against the daily ceiling."""
    s = settings()
    fabric = Fabric(s)
    with db.session() as session:
        live = select(db.Cursor).where(db.Cursor.is_demo.is_(False))
        cursors = {c.source: c for c in session.scalars(live)}
    now = db.now()
    return {
        "sources": [
            source_row(fabric, source, cursors.get(source), now, s.quiet_hours)
            for source in (s.monitor_sources or s.sources)
        ],
        "auto_tokens_today": analyses.auto_tokens_today(),
        "daily_auto_tokens": s.daily_auto_tokens,
    }


@app.get("/api/usage", dependencies=api)
def usage():
    with db.session() as s:
        rows = s.execute(
            select(
                func.date(db.LlmCall.created_at),
                db.LlmCall.service,
                func.count(),
                func.sum(db.LlmCall.requests),
                func.sum(db.LlmCall.total_tokens),
            )
            .group_by(func.date(db.LlmCall.created_at), db.LlmCall.service)
            .order_by(func.date(db.LlmCall.created_at).desc())
            .limit(90)
        ).all()
    return [
        {
            "day": str(day),
            "service": service,
            "calls": calls,
            "requests": int(requests or 0),
            "tokens": int(tokens or 0),
        }
        for day, service, calls, requests, tokens in rows
    ]
