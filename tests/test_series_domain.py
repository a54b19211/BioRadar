"""T-24 comparable domain (Codex r160): a series is one key in one domain - the topic the record was placed
in. Known different domains are never compared; an unknown domain is kept, effective and computes nothing."""

from conftest import FakeFabric, biocheck_fake, record
from sqlalchemy import select

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.research import monitor, schemas

CALL = {"stage": "triage", "model": "fake", "finish_reason": "stop", "usage": {}, "seconds": 0, "error": None}


def cases(month, value):
    sentence = f"2026年{month}月医院A在湖北省新增确诊病例{value}例。"
    return sentence, schemas.Measure(
        object_text="医院A",
        metric_text="新增确诊病例",
        value_text=str(value),
        unit_text="例",
        period_text=f"2026年{month}月",
        period=f"2026-{month:02d}",
        period_type="interval",
        kind="flow",
        scope_text="在湖北省",
        qualifier="exact",
        modality="actual",
        status="reported",
        quote=sentence,
    )


def run(monkeypatch, specs):
    """specs: (record id, topic title or "", month, value, relations); one tick per record, in order.
    Each record goes to the existing topic of that title (or opens it); "" places it in none."""
    post = biocheck_fake()
    monkeypatch.setattr(services, "biocheck_v2", lambda s, body: post(body))
    by_text = {}
    records = []
    for day, (rid, topic, month, value, relations) in enumerate(specs, 1):
        sentence, measure = cases(month, value)
        text = f"# {topic or '通报'}\n\n{sentence}"
        by_text[text] = (topic, measure)
        records.append(
            record(
                rid, topic or "通报", text, available=f"2026-06-{day:02d}T08:00:00+08:00", relations=relations
            )
        )

    def triage(settings_, stage, system, payload, model_cls, **options):
        if stage == "extract":
            return schemas.Extraction(
                items=[schemas.ExtractItem(index=w["index"]) for w in payload["windows"]]
            ), dict(CALL, stage=stage)
        index = {t["title"]: t["index"] for t in payload["topics"]}
        items = []
        for r in payload["records"]:
            topic, measure = by_text[r["text"]]
            items.append(
                schemas.TriageItem(
                    index=r["index"],
                    topic=index.get(topic) if topic else None,
                    new_topic="" if not topic or topic in index else topic,
                    direction="public_health",
                    alert="normal" if topic else "none",
                    measures=[measure],
                )
            )
        return schemas.Triage(items=items), CALL

    monkeypatch.setattr(llm, "chat_json", triage)
    for n in range(1, len(records) + 1):
        monitor.tick(is_demo=True, fabric=FakeFabric(records[:n], clean=True))
    with db.session() as s:
        topics = {t.id: t.title for t in s.scalars(select(db.Topic))}
        points = {
            p.record_id: (topics.get(p.domain_id), p.domain_basis, p.superseded_by, p.id)
            for p in s.scalars(select(db.Measurement))
        }
        events = [
            (topics[e.topic_id], e.detail["record"], sorted(c["kind"] for c in e.detail["changes"]))
            for e in s.scalars(
                select(db.TopicEvent).where(db.TopicEvent.kind == "series").order_by(db.TopicEvent.id)
            )
        ]
    return topics, points, events


def test_a_trend_stays_in_its_topic_and_another_topic_with_the_same_names_is_not_compared(monkeypatch):
    topics, points, events = run(
        monkeypatch,
        [
            ("A1", "炭疽", 1, 10, ()),
            ("A2", "炭疽", 2, 12, ()),
            ("B3", "霍乱", 3, 40, ()),  # same object, metric, unit, scope: another disease
            ("A3", "炭疽", 3, 15, ()),
            ("A4", "炭疽", 3, 18, ()),  # a second March value in the same topic: a conflict
        ],
    )
    assert {r: (t, b) for r, (t, b, _, _) in points.items()} == {
        "A1": ("炭疽", "topic"),
        "A2": ("炭疽", "topic"),
        "A3": ("炭疽", "topic"),
        "A4": ("炭疽", "topic"),
        "B3": ("霍乱", "topic"),
    }
    assert [e for e in events if e[0] == "霍乱"] == []  # its one point is compared with nothing
    by_record = {r: k for t, r, k in events}
    assert "step" in by_record["A2"] and "streak_up" in by_record["A3"]  # 10 -> 12 -> 15, not 40
    assert "conflict" in by_record["A4"]


def test_a_correction_replaces_within_the_domain_and_an_unknown_domain_computes_nothing(monkeypatch):
    topics, points, events = run(
        monkeypatch,
        [
            ("U1", "", 1, 10, ()),
            ("U2", "", 2, 30, ()),
            ("U3", "", 2, 20, [("corrects", "U2")]),
        ],
    )
    assert topics == {} and events == []
    assert {r: b for r, (_, b, _, _) in points.items()} == {"U1": "unknown", "U2": "unknown", "U3": "unknown"}
    assert points["U2"][2] == points["U3"][3]  # the correction is applied; still nothing is compared
