"""The analysis detail is an allow-list view: a stored field RunState does not mark shown never leaves the
API, whether or not anything is withheld."""

import json

from test_access import admin_client, fabric_sources  # noqa: F401 - the fixture this test takes

from bioradar import app, db
from bioradar.research import analyses


def stored_with_new_fields():
    row = analyses.create(question="XQ-17 的进展？", direction="pipeline", is_demo=True, enqueue=False)
    with db.session() as s:
        a = s.get(db.Analysis, row.id)
        a.state = "completed"
        a.progress = {
            "facts": [
                {"id": "F1", "ref": "E1", "text": "开放事实", "quote": "开放原文", "summary": "新字段原文"}
            ],
            "draft_notes": ["新键原文"],  # a key a later version might add
        }
        a.result = {
            "report_status": "PASS",
            "draft": "第一句[E1]。",
            "internal": {"prompt": "新键原文"},
            "computations": [{"id": "A1", "value": 1, "operands": [], "description": "新字段原文"}],
        }
        s.add(
            db.Evidence(
                analysis_id=row.id,
                ref="E1",
                source="open_src",
                record_id="open_src-1",
                version_id="v" * 64,
                title="open_src 标题",
                text="open_src 原文全文",
                meta={},
            )
        )
    return row.id


def test_a_field_not_marked_shown_never_leaves_the_api(fabric_sources):  # noqa: F811
    admin = admin_client()
    analysis_id = stored_with_new_fields()
    for readable in (["open_src"], []):  # nothing withheld, then the only source revoked
        fabric_sources["value"] = readable
        app._readable.update(at=None, sources=None)
        detail = admin.get(f"/api/analyses/{analysis_id}").json()
        text = json.dumps(detail, ensure_ascii=False) + admin.get(f"/api/analyses/{analysis_id}/export").text
        assert "新键原文" not in text and "新字段原文" not in text, readable
        assert "draft_notes" not in detail["progress"] and "internal" not in detail["result"]
        assert set(detail["progress"]) == {"checks", "queries", "actions", "facts", "stop", "notes"}
        assert detail["result"]["computations"] == [{"id": "A1", "value": 1, "operands": []}]
    # what is stored is untouched
    assert analyses.get(analysis_id).progress["draft_notes"] == ["新键原文"]
