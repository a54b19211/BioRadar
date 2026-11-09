"""A record's direction is the triage model's answer: left out of it, it is not filled in as public health."""

import json

from conftest import FakeFabric, record
from sqlalchemy import select
from test_monitor import DAY

from bioradar import db
from bioradar.adapters import llm
from bioradar.research import monitor


def model_answers(monkeypatch, direction):
    """The triage model's raw answer for the one record (a new topic at level important), without a direction
    when `direction` is None. It goes through the real call: the schema check and the one format retry."""
    item = {
        "index": 1,
        "new_topic": "启衡新厂房",
        "alert": "important",
        "signal": "capacity",
        "related": "related",
    }
    if direction:
        item["direction"] = direction
    content = json.dumps({"items": [item]}, ensure_ascii=False)
    answer = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}}
    monkeypatch.setattr(llm, "post", lambda s, body, call, limit=None, before_send=None: answer)


def test_a_triage_answer_without_a_direction_is_a_failed_triage_never_public_health(monkeypatch):
    fabric = FakeFabric([record("T1", "启衡生物抗体原液厂房环评公示", "x", available=DAY.format(3))])
    model_answers(monkeypatch, None)  # both answers, the format retry's too, leave the direction out
    monitor.tick(is_demo=True, fabric=fabric)
    with db.session() as s:
        [row] = s.scalars(select(db.Observation)).all()
        assert (row.reason, row.relevance, row.direction, row.topic_id) == ("triage_failed", None, None, None)
        assert s.scalars(select(db.Topic)).all() == []
    # the retry path asks again, and what the model names this time is what is stored
    model_answers(monkeypatch, "business")
    monitor.tick(is_demo=True, fabric=fabric)
    with db.session() as s:
        [row] = s.scalars(select(db.Observation)).all()
        [topic] = s.scalars(select(db.Topic)).all()
        assert (row.direction, topic.direction) == ("business", "business")
