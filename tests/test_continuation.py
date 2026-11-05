"""T-31: bounded continuation reading. A record is read past its triage window, window by window, a bounded
number per round, fairly across records; every window's state says what happened to that text, and a measure
found beyond the first window is a candidate checked like any other (one candidate, found once or twice)."""

from datetime import timedelta

import pytest
from conftest import FakeFabric, biocheck_fake, record
from sqlalchemy import select, update

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.research import continuation, measure_checks, monitor, schemas

SENTENCE = "2026年4月，国家药监局共批准注册医疗器械产品318个。"
QUOTE = "国家药监局共批准注册医疗器械产品318个"
FILL = "本公告的其他内容说明如下，详见正文。"  # no digit


def text_with(at, n=4000, sentence=SENTENCE):
    """n characters of filler with the sentence starting at `at`."""
    body = (FILL * (n // len(FILL) + 2))[:n]
    return body[:at] + sentence + body[at + len(sentence) :][: n - at - len(sentence)]


def measure(**over):
    base = dict(
        object_text="国家药监局",
        metric_text="批准注册医疗器械产品",
        value_text="318",
        unit_text="个",
        period_text="2026年4月",
        period="2026-04",
        period_type="interval",
        kind="flow",
        status="reported",
        quote=QUOTE,
    )
    base.update(over)
    return schemas.Measure(**base)


def model(calls, *, error_for=None, extract_over=None):
    """Triage and extract stand-in: a measure for every window / record whose text holds the whole quote.
    error_for(n) -> an error code for the n-th extract call (no result), else None."""
    n = {"extract": 0}

    def ok(stage):
        return {
            "stage": stage,
            "model": "fake",
            "finish_reason": "stop",
            "usage": {},
            "seconds": 0,
            "error": None,
        }

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        calls.append((stage, payload))
        if stage == "extract":
            n["extract"] += 1
            err = error_for(n["extract"]) if error_for else None
            if err:
                return None, dict(ok(stage), error=err)
            return schemas.Extraction(
                items=[
                    schemas.ExtractItem(
                        index=w["index"],
                        measures=[measure(**(extract_over or {}))] if QUOTE in w["text"] else [],
                    )
                    for w in payload["windows"]
                ]
            ), ok(stage)
        return schemas.Triage(
            items=[
                schemas.TriageItem(
                    index=r["index"],
                    alert="normal",
                    new_topic="批准",
                    measures=[measure()] if QUOTE in r["text"] else [],
                )
                for r in payload["records"]
            ]
        ), ok(stage)

    return chat_json


@pytest.fixture
def stand_ins(monkeypatch):
    calls, checks = [], []
    post = biocheck_fake(calls=checks)
    monkeypatch.setattr(services, "biocheck_v2", lambda s, body: post(body))
    monkeypatch.setattr(llm, "chat_json", model(calls))
    return calls, checks


def plans():
    with db.session() as s:
        return [
            (r.version_id, r.state, r.reason, r.read_until, r.windows_total)
            for r in s.scalars(select(db.RecordRead).order_by(db.RecordRead.id))
        ]


def windows(read_index=0):
    with db.session() as s:
        read = list(s.scalars(select(db.RecordRead).order_by(db.RecordRead.id)))[read_index]
        return [
            (w.window_index, w.start_char, w.end_char, w.state, w.attempts)
            for w in s.scalars(
                select(db.RecordReadWindow)
                .where(db.RecordReadWindow.read_id == read.id)
                .order_by(db.RecordReadWindow.window_index)
            )
        ]


def coverage(read_index=0):
    with db.session() as s:
        read = list(s.scalars(select(db.RecordRead).order_by(db.RecordRead.id)))[read_index]
        return continuation.coverage(s, read)


def candidates():
    with db.session() as s:
        return [
            (c.status, c.quote_start, c.quote_end)
            for c in s.scalars(select(db.MeasurementCandidate).order_by(db.MeasurementCandidate.id))
        ]


def due():
    with db.session() as s:
        s.execute(update(db.RecordReadWindow).values(next_at=db.now() - timedelta(seconds=1)))
        s.execute(update(db.MeasurementCheckTask).values(next_check_at=db.now() - timedelta(seconds=1)))


def feed(*texts, clean=True):
    return FakeFabric(
        [record(f"R{i}", f"记录{i}", t, available="2026-05-08T08:00:00+08:00") for i, t in enumerate(texts)],
        clean=clean,
    )


def effective():
    with db.session() as s:
        return [m.value_text for m in s.scalars(measure_checks.effective(s, monitor.settings(), True))]


def test_the_triage_window_is_continuation_window_zero():
    assert monitor.TRIAGE_CHARACTERS == continuation.WINDOW == 1500
    assert continuation.window_starts(4000, 300) == [0, 1200, 2400, 3600]
    assert continuation.window_starts(1500, 300) == [0]


def test_a_measure_beyond_the_first_window_is_read_checked_and_effective(stand_ins):
    calls, _ = stand_ins
    fabric = feed(text_with(3000))
    monitor.tick(is_demo=True, fabric=fabric)
    # round 1: triage window 0, then windows 1 and 2 (two per record per round); the measure is in window 2
    assert [w[3] for w in windows()] == ["sent", "sent", "sent", "pending"]
    q0 = 3000 + SENTENCE.index(QUOTE)
    assert [c[1:] for c in candidates()] == [(q0, q0 + len(QUOTE))]
    due()
    monitor.tick(is_demo=True, fabric=fabric)
    assert [w[3] for w in windows()] == ["sent"] * 4
    assert plans()[0][1:4] == ("complete", None, 4000)
    assert effective() == ["318"]
    extract = [p for st, p in calls if st == "extract"]
    assert [[w["text"] for w in p["windows"]] for p in extract][0][0] == text_with(3000)[1200:2700]
    with db.session() as s:  # every window sent is a real material read bound to the artifact
        reads = list(s.scalars(select(db.MaterialRead).where(db.MaterialRead.stage == "extract")))
        assert [(r.status, r.chars) for r in reads] == [("sent", 1500), ("sent", 1500), ("sent", 400)]
        assert all(seg["part"]["kind"] == "clean_text" for r in reads for seg in r.binding["segments"])


def test_a_sentence_in_the_overlap_found_twice_is_one_candidate(stand_ins):
    monitor.tick(is_demo=True, fabric=feed(text_with(1250, n=2600)))
    assert len(candidates()) == 1  # triage found it in window 0, window 1 found it again
    with db.session() as s:
        found = [
            (a.window_index, a.measures_found, a.measures_enqueued)
            for a in s.scalars(select(db.RecordReadAttempt).where(db.RecordReadAttempt.outcome == "sent"))
        ]
    assert found == [(1, 1, 0)]


def test_a_sentence_across_the_first_border_is_found_once_by_the_next_window(stand_ins):
    at = 1490  # the quote runs past 1500: not in the triage window, whole in window 1
    monitor.tick(is_demo=True, fabric=feed(text_with(at, n=2600)))
    q0 = at + SENTENCE.index(QUOTE)
    assert [c[1:] for c in candidates()] == [(q0, q0 + len(QUOTE))]


def test_a_long_record_is_capped_and_says_what_was_not_read(stand_ins, monkeypatch):
    monkeypatch.setattr(monitor.settings(), "continuation_max_windows", 2)
    fabric = feed(text_with(100, n=6000))
    monitor.tick(is_demo=True, fabric=fabric)
    assert plans()[0][1] == "capped"
    cov = coverage()
    assert cov["model_read_chars"] == 3900 and cov["unread_chars"] == 2100 and cov["windows"]["capped"] == 2


def test_reading_is_fair_across_records(stand_ins, monkeypatch):
    calls, _ = stand_ins
    monkeypatch.setattr(monitor.settings(), "continuation_calls_per_round", 1)
    monitor.tick(is_demo=True, fabric=feed(text_with(100, n=9000), text_with(100, n=2600)))
    first = [p for st, p in calls if st == "extract"][0]
    # one call: the long record's window 1, the short one's window 1, then the long one's window 2
    assert [w["position"] for w in first["windows"]] == [
        "1200-2700 / 9000",
        "1200-2600 / 2600",
        "2400-3900 / 9000",
    ]


def test_a_withdrawn_permission_pauses_the_plan_and_it_reads_on_when_restored(stand_ins):
    fabric = feed(text_with(3000))
    monitor.tick(is_demo=True, fabric=fabric)
    fabric.revoked.add(fabric.docs[0]["version_id"])
    due()
    monitor.tick(is_demo=True, fabric=fabric)
    assert plans()[0][1:3] == ("paused_unreadable", "source_unreadable")
    assert windows()[3][3] == "source_unreadable" and windows()[3][4] == 0  # no attempt spent
    fabric.revoked.clear()
    due()
    monitor.tick(is_demo=True, fabric=fabric)
    assert plans()[0][1] == "complete"


def test_a_spent_model_budget_leaves_windows_owed_without_spending_attempts(monkeypatch):
    calls = []
    post = biocheck_fake()
    monkeypatch.setattr(services, "biocheck_v2", lambda s, body: post(body))
    monkeypatch.setattr(
        llm, "chat_json", model(calls, error_for=lambda n: "http_error:429" if n == 1 else None)
    )
    fabric = feed(text_with(3000))
    monitor.tick(is_demo=True, fabric=fabric)
    assert [w[3:] for w in windows()][1:3] == [("budget_spent", 0), ("budget_spent", 0)]
    for _ in range(2):  # windows 1 and 2, then window 3
        due()
        monitor.tick(is_demo=True, fabric=fabric)
    assert plans()[0][1] == "complete" and len(candidates()) == 1


def test_failures_are_retried_a_bounded_number_of_times(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "biocheck_v2", lambda s, body: biocheck_fake()(body))
    monkeypatch.setattr(llm, "chat_json", model(calls, error_for=lambda n: "timeout"))
    fabric = feed(text_with(100, n=2600))
    for _ in range(4):
        monitor.tick(is_demo=True, fabric=fabric)
        due()
    assert windows()[1][3:] == ("failed_final", 3)
    assert plans()[0][1] == "failed"
    assert len([c for c in calls if c[0] == "extract"]) == 3  # bounded


def test_a_short_record_is_complete_with_its_triage_window(stand_ins):
    monitor.tick(is_demo=True, fabric=feed(SENTENCE))
    assert plans()[0][1] == "complete" and windows() == [(0, 0, len(SENTENCE), "sent", 0)]


def test_text_that_is_not_one_cleaned_artifact_is_not_planned_and_says_so(stand_ins):
    monitor.tick(is_demo=True, fabric=feed(text_with(3000), clean=False))
    assert plans()[0][1:4] == ("not_planned", "not_a_clean_artifact", 1500)
    assert coverage()["unread_chars"] == 2500


def test_windows_without_a_digit_are_skipped_only_when_configured(stand_ins, monkeypatch):
    calls, _ = stand_ins
    monkeypatch.setattr(monitor.settings(), "continuation_skip_no_digit", True)
    monitor.tick(is_demo=True, fabric=feed(text_with(3000)))
    assert [w[3] for w in windows()][:3] == ["sent", "skipped_no_digit", "sent"]
    cov = coverage()
    assert cov["skipped_no_digit_chars"] > 0 and cov["model_read_chars"] < 4000


def test_attempts_are_append_only(stand_ins):
    monitor.tick(is_demo=True, fabric=feed(text_with(3000)))
    with db.session() as s:
        n = len(list(s.scalars(select(db.RecordReadAttempt))))
    assert n == 3  # window 0 by triage, windows 1 and 2


def test_the_same_statement_read_differently_in_an_overlap_is_still_one_candidate(monkeypatch):
    """Window 1 states the overlap's measure with another status: same place, same value and metric - one."""
    calls = []
    monkeypatch.setattr(services, "biocheck_v2", lambda s, body: biocheck_fake()(body))
    monkeypatch.setattr(llm, "chat_json", model(calls, extract_over={"status": "confirmed"}))
    monitor.tick(is_demo=True, fabric=feed(text_with(1250, n=2600)))
    assert len(candidates()) == 1
