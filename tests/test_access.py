"""T-14: people and roles, human reviews, and material from sources Fabric no longer lets us read."""

import pytest
from fastapi.testclient import TestClient

from bioradar import app as api_module
from bioradar import db
from bioradar.app import app
from bioradar.config import settings
from bioradar.research import analyses, monitor


def admin_client():
    client = TestClient(app)
    assert client.post("/api/session", json={"credential": settings().dev_credential}).status_code == 200
    return client


def signed_in(name, credential):
    client = TestClient(app)
    assert client.post("/api/session", json={"name": name, "credential": credential}).status_code == 200
    return client


def test_the_dev_credential_creates_the_first_admin_and_roles_limit_what_each_person_can_do():
    admin = admin_client()
    assert admin.get("/api/session").json() == {"signed_in": True, "name": "admin", "role": "admin"}
    for name, role in (("wang", "viewer"), ("li", "analyst")):
        made = admin.post("/api/users", json={"name": name, "role": role, "credential": f"{name}-secret-1"})
        assert made.status_code == 200
    assert (
        admin.post("/api/users", json={"name": "li", "role": "viewer", "credential": "x" * 8}).status_code
        == 409
    )

    viewer, analyst = signed_in("wang", "wang-secret-1"), signed_in("li", "li-secret-1")
    assert viewer.get("/api/options").status_code == 200
    assert viewer.post("/api/reviews", json={"action": "set_alert"}).status_code == 403
    assert viewer.post("/api/monitor/tick", json={}).status_code == 403
    assert analyst.get("/api/users").status_code == 403
    assert (
        TestClient(app).post("/api/session", json={"name": "li", "credential": "wrong-one"}).status_code
        == 401
    )

    # a role change takes effect on the next request, without signing in again
    assert admin.patch("/api/users/wang", json={"role": "analyst"}).status_code == 200
    assert viewer.post("/api/reviews", json={"action": "set_alert"}).status_code == 422  # allowed, bad input
    # the last admin cannot be demoted; once the admin replaces the credential the dev one stops working
    assert admin.patch("/api/users/admin", json={"role": "viewer"}).status_code == 409
    assert admin.patch("/api/users/admin", json={"credential": "a-new-admin-secret"}).status_code == 200
    assert (
        TestClient(app).post("/api/session", json={"credential": settings().dev_credential}).status_code
        == 401
    )
    signed_in("admin", "a-new-admin-secret")


def test_a_tampered_or_foreign_token_is_not_a_session():
    import hmac

    admin = admin_client()
    token = admin.cookies.get(api_module.COOKIE)
    user_id, version, expires, signature = token.split(".")
    body = f"{user_id}.{version}.{expires}"
    # Codex r20: with no session key the dev credential used to sign, so whoever knew it could mint an admin
    by_dev = hmac.new(settings().dev_credential.encode(), body.encode(), "sha256").hexdigest()
    for bad in (
        f"{user_id}.{version}.{int(expires) + 999}.{signature}",
        f"{user_id}.{int(version) + 1}.{expires}.{signature}",
        f"{body}.",
        f"{body}.{by_dev}",
        "x",
        settings().dev_credential,
    ):
        assert (
            TestClient(app).get("/api/options", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
        )
    assert (
        TestClient(app).get("/api/options", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    )


def test_a_new_credential_or_deactivation_ends_older_sessions_at_once():
    """Codex r20: after alice's credential was replaced her old browser still read protected pages."""
    admin = admin_client()
    admin.post("/api/users", json={"name": "alice", "role": "analyst", "credential": "alice-pass-1"})
    old = signed_in("alice", "alice-pass-1")
    assert old.get("/api/options").status_code == 200
    assert admin.patch("/api/users/alice", json={"credential": "alice-pass-2"}).status_code == 200
    assert old.get("/api/options").status_code == 401
    assert old.get("/api/session").json() == {"signed_in": False}
    again = signed_in("alice", "alice-pass-2")
    assert again.get("/api/options").status_code == 200
    # a role change keeps the session (it is re-read on every request)
    assert admin.patch("/api/users/alice", json={"role": "viewer"}).status_code == 200
    assert again.get("/api/options").status_code == 200
    # deactivation ends it, and the person cannot sign in until reactivated
    assert admin.patch("/api/users/alice", json={"active": False}).status_code == 200
    assert again.get("/api/options").status_code == 401
    off = TestClient(app).post("/api/session", json={"name": "alice", "credential": "alice-pass-2"})
    assert off.status_code == 401
    assert admin.patch("/api/users/alice", json={"active": True}).status_code == 200
    signed_in("alice", "alice-pass-2")
    # the last active admin can be neither demoted nor deactivated
    assert admin.patch("/api/users/admin", json={"active": False}).status_code == 409


def test_without_a_session_key_nobody_signs_in(monkeypatch):
    monkeypatch.setattr(settings(), "session_secret", "")
    response = TestClient(app).post("/api/session", json={"credential": settings().dev_credential})
    assert response.status_code == 503


def topic_analysis():
    """A completed analysis in a topic, with two sentences checked by BioCheck and two materials."""
    with db.session() as s:
        topic = db.Topic(
            workspace_id="default",
            title="XQ-17",
            direction="pipeline",
            is_demo=True,
            first_seen_at=db.now(),
            last_activity_at=db.now(),
        )
        s.add(topic)
        s.flush()
        topic_id = topic.id
    row = analyses.create(
        question="XQ-17 的进展？", direction="pipeline", is_demo=True, topic_id=topic_id, enqueue=False
    )
    with db.session() as s:
        a = s.get(db.Analysis, row.id)
        a.state = "completed"
        a.result = {
            "draft": "第一句[E1]。第二句撤权原文[E2]。",
            "report_status": "PASS",
            "assessment": {
                "alert": "important",
                "judgments": [
                    {
                        "question": "是否重要",
                        "conclusion": "据撤权原文判断重要",
                        "confidence": "possible",
                        "credibility": "medium",
                        "consequence": "medium",
                        "priority": "high",
                        "grounds": ["E2"],
                        "unproven": "撤权原文以外的佐证",
                        "check": "supported",
                    }
                ],
            },
            "mechanism": {"status": "completed", "overall": "撤权原文支持该机制"},
            "review": {
                "claims": [
                    {
                        "index": 1,
                        "text": "第一句[E1]。",
                        "quotes": [{"ref": "E1", "quote": "开放原文", "found": True}],
                    },
                    {
                        "index": 2,
                        "text": "第二句撤权原文[E2]。",
                        "quotes": [{"ref": "E2", "quote": "撤权原文", "found": True}],
                    },
                ]
            },
            "computations": [{"id": "C1", "operands": [{"fact": "F2", "ref": "E2", "quote": "撤权原文"}]}],
        }
        a.progress = {
            "facts": [
                {
                    "id": "F1",
                    "ref": "E1",
                    "text": "开放事实",
                    "quote": "开放原文",
                    "checks": [],
                    "stance": "supports",
                },
                {
                    "id": "F2",
                    "ref": "E2",
                    "text": "撤权事实",
                    "quote": "撤权原文",
                    "checks": [],
                    "stance": "supports",
                },
            ]
        }
        for ref, source in (("E1", "open_src"), ("E2", "gone_src")):
            s.add(
                db.Evidence(
                    analysis_id=row.id,
                    ref=ref,
                    source=source,
                    record_id=f"{source}-1",
                    version_id="v" * 64,
                    title=f"{source} 标题",
                    text=f"{source} 原文全文",
                    meta={"url": f"https://example.org/{source}"},
                )
            )
        s.add(
            db.Observation(
                workspace_id="default",
                source="gone_src",
                record_id="gone_src-1",
                version_id="v" * 64,
                title="撤权原文标题",
                is_demo=True,
                available_at=db.now(),
                alert="important",
                closure=False,
                reason="分诊：撤权原文提到聚集",
                topic_id=topic_id,
            )
        )
        s.add(
            db.TopicEvent(
                topic_id=topic_id,
                at=db.now(),
                kind="created",
                detail={"record": "gone_src-1", "title": "撤权原文标题"},
            )
        )
    return row.id, topic_id


def test_rejecting_a_sentence_or_flagging_a_material_owes_the_topic_a_review_analysis():
    admin = admin_client()
    admin.post("/api/users", json={"name": "li", "role": "analyst", "credential": "li-secret-1"})
    analyst = signed_in("li", "li-secret-1")
    analysis_id, topic_id = topic_analysis()

    confirm = analyst.post(
        "/api/reviews", json={"action": "confirm", "analysis_id": analysis_id, "sentence": 1}
    )
    assert confirm.status_code == 200 and confirm.json()["by"] == "li"
    with db.session() as s:
        assert s.get(db.Topic, topic_id).pending_reason is None  # confirming owes nothing

    assert (
        analyst.post(
            "/api/reviews", json={"action": "reject", "analysis_id": analysis_id, "sentence": 9}
        ).status_code
        == 422
    )
    assert (
        analyst.post(
            "/api/reviews", json={"action": "flag_source", "analysis_id": analysis_id, "evidence_ref": "E7"}
        ).status_code
        == 422
    )
    rejected = {"action": "reject", "analysis_id": analysis_id, "sentence": 2, "note": "HR 写反了"}
    assert analyst.post("/api/reviews", json=rejected).status_code == 200
    flagged = {
        "action": "flag_source",
        "analysis_id": analysis_id,
        "evidence_ref": "E1",
        "note": "转载不是原件",
    }
    assert analyst.post("/api/reviews", json=flagged).status_code == 200
    alert = {"action": "set_alert", "topic_id": topic_id, "alert": "urgent"}
    assert analyst.post("/api/reviews", json=alert).status_code == 200

    with db.session() as s:
        topic = s.get(db.Topic, topic_id)
        assert topic.pending_reason == "analyst_review" and topic.alert == "urgent"
        basis = monitor.review_basis(s, topic_id, topic.pending_since)
    assert "报告第 2 句被驳回" in basis and "HR 写反了" in basis and "材料 E1 被标记有误" in basis
    assert "第 1 句" not in basis  # a confirmation is not something to answer

    detail = admin.get(f"/api/analyses/{analysis_id}").json()
    assert [r["action"] for r in detail["reviews"]] == ["flag_source", "reject", "confirm"]
    assert [r["action"] for r in admin.get(f"/api/reviews?topic_id={topic_id}").json()][0] == "set_alert"


@pytest.fixture
def fabric_sources(monkeypatch):
    """Fabric's source list as the API process sees it; None makes the list unreadable."""
    listed = {"value": ["open_src"]}

    class Fake:
        def __init__(self, s):
            pass

        def sources(self):
            if listed["value"] is None:
                raise api_module.FabricError("fabric_http_503", "down")
            return {"business_types": [], "sources": [{"source_id": x} for x in listed["value"]]}

    monkeypatch.setattr(api_module, "Fabric", Fake)
    monkeypatch.setattr(api_module, "_readable", {"at": None, "sources": None})
    return listed


def test_a_revoked_source_shows_no_text_in_any_exit(fabric_sources):
    admin = admin_client()
    analysis_id, topic_id = topic_analysis()

    assert admin.get(f"/api/analyses/{analysis_id}/evidence/E1").json()["text"] == "open_src 原文全文"
    gone = admin.get(f"/api/analyses/{analysis_id}/evidence/E2").json()
    assert gone["withheld"] == "来源已撤权" and gone["text"] == "" and gone["title"] == "" and not gone["url"]
    assert gone["record_id"] == "gone_src-1"

    detail = admin.get(f"/api/analyses/{analysis_id}").json()
    evidence = {e["ref"]: e for e in detail["evidence"]}
    assert evidence["E1"]["title"] == "open_src 标题" and "withheld" not in evidence["E1"]
    assert evidence["E2"]["title"] == "" and evidence["E2"]["withheld"] == "来源已撤权"
    facts = {f["ref"]: f for f in detail["progress"]["facts"]}
    assert facts["E1"]["quote"] == "开放原文" and facts["E2"]["quote"] == "" and facts["E2"]["text"] == ""
    assert detail["result"]["computations"][0]["operands"][0]["quote"] == ""
    # Codex r20: the same words also sat in the draft, the sentence checks, the judgments and the export
    result = detail["result"]
    assert "E2（来源已撤权）" in result["withheld"] and result["draft"] == ""
    judgment = result["assessment"]["judgments"][0]
    assert judgment["conclusion"] == "" and judgment["confidence"] == "possible"
    assert judgment["grounds"] == ["E2"]
    assert [c["index"] for c in result["review"]["claims"]] == [1, 2]
    import json

    for exit_ in (
        json.dumps(detail, ensure_ascii=False),
        json.dumps(admin.get("/api/analyses").json(), ensure_ascii=False),
        admin.get(f"/api/analyses/{analysis_id}/export").text,
        json.dumps(admin.get("/api/topics?is_demo=true").json(), ensure_ascii=False),
        json.dumps(admin.get(f"/api/topics/{topic_id}").json(), ensure_ascii=False),
        json.dumps(admin.get("/api/monitor/observations?is_demo=true").json(), ensure_ascii=False),
    ):
        assert "撤权原文" not in exit_ and "gone_src 标题" not in exit_
    export = admin.get(f"/api/analyses/{analysis_id}/export").text
    assert "open_src 标题" in export and "[E2] 来源已撤权（gone_src gone_src-1）" in export
    topic = admin.get(f"/api/topics/{topic_id}").json()
    assert topic["withheld"] == "来源已撤权" and topic["observations"][0]["record_id"] == "gone_src-1"
    with db.session() as s:  # nothing stored was changed; only what leaves the API
        assert s.get(db.Analysis, analysis_id).progress["facts"][1]["quote"] == "撤权原文"


def test_when_the_source_list_cannot_be_read_no_text_leaves_the_api(fabric_sources):
    admin = admin_client()
    analysis_id, _ = topic_analysis()
    fabric_sources["value"] = None
    item = admin.get(f"/api/analyses/{analysis_id}/evidence/E1").json()
    assert item["text"] == "" and item["withheld"] == "来源授权状态暂时无法确认"
    # a failed read is not cached: once Fabric answers again the text comes back
    fabric_sources["value"] = ["open_src"]
    assert admin.get(f"/api/analyses/{analysis_id}/evidence/E1").json()["text"] == "open_src 原文全文"
    # within five minutes the cached list is used; after that it is read again
    fabric_sources["value"] = []
    assert admin.get(f"/api/analyses/{analysis_id}/evidence/E1").json()["text"] == "open_src 原文全文"
    api_module._readable["at"] -= api_module.READABLE_SECONDS
    assert admin.get(f"/api/analyses/{analysis_id}/evidence/E1").json()["withheld"] == "来源已撤权"
