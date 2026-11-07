"""Proposition numbers (research/propositions.py): a judgment keeps its id through the analyses after it."""

from datetime import UTC, datetime

from sqlalchemy import select

from bioradar import db
from bioradar.config import settings
from bioradar.research import analyses, propositions

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def topic_row(session):
    topic = db.Topic(
        workspace_id=settings().workspace_id,
        title="t",
        direction="public_health",
        is_demo=True,
        first_seen_at=NOW,
        last_activity_at=NOW,
    )
    session.add(topic)
    session.flush()
    return topic


def analysis_row(session, topic, *, parent=None, result=None):
    """An analysis of the topic (a topic's analyses each open an investigation of their own)."""
    investigation = db.Investigation(
        workspace_id=settings().workspace_id,
        question="q",
        direction="public_health",
        is_demo=True,
        topic_id=topic.id if topic else None,
    )
    session.add(investigation)
    session.flush()
    row = db.Analysis(
        investigation_id=investigation.id,
        parent_id=parent.id if parent else None,
        trigger="manual",
        method="planner",
        input={"question": "q", "is_demo": True},
        input_hash=db.new_id(),
        result=result,
    )
    session.add(row)
    session.flush()
    return row


def judgment(question, conclusion="待证实", **fields):
    return {"question": question, "conclusion": conclusion, "confidence": "possible", "grounds": [], **fields}


def result_of(*judgments, group="G-1"):
    return {"assessment": {"judgments": list(judgments)}, "groups": {"display": group}}


def follow(session, topic, parent, *judgments, group="G-1"):
    """The next analysis as execute stores it: its judgments numbered, then kept as its result."""
    row = analysis_row(session, topic, parent=parent)
    result = result_of(*judgments, group=group)
    propositions.number(session, row, result)
    row.result = result
    session.flush()
    return row


def ids(row):
    return [(j["prop_id"], j["content_rev"]) for j in row.result["assessment"]["judgments"]]


def test_judgments_are_numbered_from_the_topic_and_say_what_they_depend_on():
    with db.session() as s:
        topic = topic_row(s)
        row = analysis_row(s, topic)
        s.add(db.LlmCall(analysis_id=row.id, stage="verify", service="biocheck"))
        s.add(
            db.Evidence(
                analysis_id=row.id,
                ref="E1",
                source="x",
                record_id="r",
                version_id="v",
                title="t",
                text="x",
                meta={"material_read_id": 7},
            )
        )
        s.flush()
        call = s.scalar(select(db.LlmCall.id).where(db.LlmCall.analysis_id == row.id))
        checked = judgment("是否人传人", grounds=["E1", "E2"], check="supported")
        unchecked = judgment("病原是什么", check="unverified")

        propositions.number(s, row, result_of(checked, unchecked))

        short = topic.id[:8]
        assert [checked["prop_id"], unchecked["prop_id"]] == [f"T{short}-P1", f"T{short}-P2"]
        assert topic.next_prop_seq == 3
        assert checked["depends_on"] == {"material_reads": [7], "checked_by": call}  # E2 was never read
        assert unchecked["depends_on"]["checked_by"] is None  # no check of its conclusion happened


def test_the_same_question_and_scope_keep_their_id_and_a_new_conclusion_raises_the_rev():
    with db.session() as s:
        topic = topic_row(s)
        first = follow(s, topic, None, judgment("是否人传人", "尚无证据"), judgment("病原是什么", "未知"))
        again = judgment("是否人传人", "尚无证据")
        second = follow(s, topic, first, again, judgment("病原是什么  ", "埃博拉"))
        # the third leaves the first question out and asks a new one
        third = follow(s, topic, second, judgment("病原是什么", "埃博拉"), judgment("会否扩散", "未知"))

        assert [i for i, _ in ids(first)] == [i for i, _ in ids(second)]
        assert ids(second)[0][1] == 1 and ids(second)[1][1] == 2  # only the changed conclusion
        assert ids(third)[0] == ids(second)[1]  # the same words again, nothing changed
        assert ids(third)[1][0].endswith("-P3") and ids(third)[1][1] == 1
        assert topic.next_prop_seq == 4


def test_a_changed_scope_starts_a_new_id_naming_the_old_and_explanations_do_not_displace_each_other():
    with db.session() as s:
        topic = topic_row(s)
        first = follow(s, topic, None, judgment("解释甲：输入性病例"), judgment("解释乙：本地传播"))
        moved = follow(s, topic, first, judgment("解释甲：输入性病例"), group="G-2")
        only_b = follow(s, topic, first, judgment("解释乙：本地传播"))

        (a, _), (b, _) = ids(first)
        [(new_a, _)] = ids(moved)
        [(same_b, _)] = ids(only_b)
        assert new_a not in (a, b) and moved.result["assessment"]["judgments"][0]["derived_from"] == a
        assert same_b == b  # B did not take A's id, nor A the one of B
        assert "derived_from" not in first.result["assessment"]["judgments"][0]


def test_an_analysis_without_a_topic_is_left_as_it_is():
    with db.session() as s:
        row = analysis_row(s, None)
        j = judgment("是否人传人")

        propositions.number(s, row, result_of(j))

        assert "prop_id" not in j


def test_execute_numbers_the_result_in_the_transaction_that_stores_it(monkeypatch):
    with db.session() as s:
        topic = topic_row(s)
        row = analysis_row(s, topic)
        analysis_id, topic_id = row.id, topic.id

    class StubRun:
        def __init__(self, analysis, settings_, lease_token=None):
            pass

        def run(self):
            return {"report_status": "PASS", **result_of(judgment("是否人传人"), judgment("病原是什么"))}

    monkeypatch.setattr(analyses, "Run", StubRun)

    analyses.execute(analysis_id)

    with db.session() as s:
        stored = s.get(db.Analysis, analysis_id)
        topic = s.get(db.Topic, topic_id)
        assert stored.state == "completed"
        assert [j["prop_id"] for j in stored.result["assessment"]["judgments"]] == [
            f"T{topic_id[:8]}-P1",
            f"T{topic_id[:8]}-P2",
        ]
        assert topic.next_prop_seq == 3
