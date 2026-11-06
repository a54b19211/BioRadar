"""T-31: bounded continuation reading. A record is read past its triage window, window by window, a bounded
number per round, fairly across sources and records; every window's state says what happened to that text,
and a measure found beyond the first window is a candidate checked like any other - located in the window
that was read, once even when an overlap shows it twice, never reused across cleaned artifacts."""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeFabric, biocheck_fake, record
from sqlalchemy import select, update

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.research import continuation, measure_checks, monitor, schemas

SENTENCE = "2026年4月，国家药监局共批准注册医疗器械产品318个。"
QUOTE = "国家药监局共批准注册医疗器械产品318个"
FILL = "本公告的其他内容说明如下，详见正文。"  # no digit; a sentence end every 18 characters


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


def model(calls, *, error_for=None, extract_over=None, quote=QUOTE, by_quote=None):
    """Triage and extract stand-in: a measure for every window / record whose text holds the whole quote.
    error_for(n) -> an error code for the n-th extract call (no result), else None. extract_over: fields a
    continuation window states differently from triage. by_quote: {quote: measure fields}."""
    n = {"extract": 0}
    by_quote = by_quote or {quote: {}}

    def ok(stage):
        return {
            "stage": stage,
            "model": "fake",
            "finish_reason": "stop",
            "usage": {},
            "seconds": 0,
            "error": None,
        }

    def found(text, extra=None):
        return [measure(**{"quote": q, **kw, **(extra or {})}) for q, kw in by_quote.items() if q in text]

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        calls.append((stage, payload))
        if stage == "extract":
            n["extract"] += 1
            err = error_for(n["extract"]) if error_for else None
            if err:
                return None, dict(ok(stage), error=err)
            items = [
                schemas.ExtractItem(index=w["index"], measures=found(w["text"], extract_over))
                for w in payload["windows"]
            ]
            return schemas.Extraction(items=items), ok(stage)
        items = [
            schemas.TriageItem(index=r["index"], alert="normal", new_topic="批准", measures=found(r["text"]))
            for r in payload["records"]
        ]
        return schemas.Triage(items=items), ok(stage)

    return chat_json


@pytest.fixture
def stand_ins(monkeypatch):
    calls, checks = [], []
    post = biocheck_fake(calls=checks)
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: post(body))
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
            (c.status, c.quote_start, c.quote_end, c.reason)
            for c in s.scalars(select(db.MeasurementCandidate).order_by(db.MeasurementCandidate.id))
        ]


def due():
    with db.session() as s:
        s.execute(update(db.RecordReadWindow).values(next_at=db.now() - timedelta(seconds=1)))
        s.execute(update(db.MeasurementCheckTask).values(next_check_at=db.now() - timedelta(seconds=1)))


def feed(*texts, clean=True, sources=None):
    return FakeFabric(
        [
            record(
                f"R{i}",
                f"记录{i}",
                t,
                available="2026-05-08T08:00:00+08:00",
                source=(sources or {}).get(i, "external_archive"),
            )
            for i, t in enumerate(texts)
        ],
        clean=clean,
    )


def effective():
    with db.session() as s:
        return [m.value_text for m in s.scalars(measure_checks.effective(s, monitor.settings(), True))]


def q_at(at):
    q0 = at + SENTENCE.index(QUOTE)
    return (q0, q0 + len(QUOTE))


def ticks(fabric, n):
    for _ in range(n):
        monitor.tick(is_demo=True, fabric=fabric)
        due()


# ---- windows ---------------------------------------------------------------------------------------------


def test_window_zero_is_the_triage_window_and_later_ones_cut_at_real_boundaries():
    assert monitor.TRIAGE_CHARACTERS == continuation.WINDOW == measure_checks.TRIAGE_WINDOW == 1500
    text = text_with(3000, n=6000)
    spans = continuation.windows_of(text, 300)
    assert spans[0] == (0, 1500) and spans[-1][1] == len(text)
    for (_, b0), (a, b) in zip(spans, spans[1:]):
        assert b0 - 300 <= a < b0  # starts inside the overlap
        assert text[a - 1] in "。\n"  # at a sentence start
        assert b == len(text) or text[b - 1] in "。\n"  # and ends at a sentence end
        assert 1000 <= b - a <= 1500
    lines = "\n".join(["| 2026-04 | 318 |"] * 200)  # a table: rows are the boundaries
    for a, b in continuation.windows_of(lines, 300)[1:]:
        assert lines[a - 1] == "\n" and (b == len(lines) or lines[b - 1] == "\n")
    assert continuation.windows_of(SENTENCE, 300) == [(0, len(SENTENCE))]


def test_text_without_any_boundary_is_still_cut_into_bounded_windows():
    assert continuation.windows_of("国" * 4000, 300) == [(0, 1500), (1200, 2700), (2400, 3900), (3600, 4000)]


# ---- reading, checking ------------------------------------------------------------------------------------


def test_a_measure_beyond_the_first_window_is_read_checked_and_effective(stand_ins):
    calls, _ = stand_ins
    text = text_with(3000)
    spans = continuation.windows_of(text, 300)
    fabric = feed(text)
    monitor.tick(is_demo=True, fabric=fabric)
    assert [w[1:3] for w in windows()] == spans
    assert [w[3] for w in windows()] == ["sent", "sent", "sent"] + ["pending"] * (len(spans) - 3)
    assert [c[1:3] for c in candidates()] == [q_at(3000)]
    ticks(fabric, len(spans))
    assert plans()[0][1:4] == ("complete", None, 4000)
    assert effective() == ["318"]
    extract = [p for st, p in calls if st == "extract"]
    assert extract[0]["windows"][0]["text"] == text[spans[1][0] : spans[1][1]]
    with db.session() as s:  # every window sent is a real material read bound to the artifact
        reads = list(s.scalars(select(db.MaterialRead).where(db.MaterialRead.stage == "extract")))
        assert [(r.status, r.chars) for r in reads] == [("sent", b - a) for a, b in spans[1:]]
        assert all(seg["part"]["kind"] == "clean_text" for r in reads for seg in r.binding["segments"])


def test_a_sentence_in_the_overlap_found_twice_is_one_candidate(stand_ins):
    monitor.tick(is_demo=True, fabric=feed(text_with(1300, n=2600)))
    assert len(candidates()) == 1  # triage found it in window 0, window 1 found it again
    with db.session() as s:
        found = [
            (a.window_index, a.measures_found, a.measures_enqueued)
            for a in s.scalars(select(db.RecordReadAttempt).where(db.RecordReadAttempt.outcome == "sent"))
        ]
    assert found == [(1, 1, 0)]


def test_the_same_statement_read_differently_in_an_overlap_is_still_one_candidate(monkeypatch):
    """Window 1 states the overlap's measure with another status: same place, same value and metric - one."""
    calls = []
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: biocheck_fake()(body))
    monkeypatch.setattr(llm, "chat_json", model(calls, extract_over={"status": "confirmed"}))
    monitor.tick(is_demo=True, fabric=feed(text_with(1300, n=2600)))
    assert len(candidates()) == 1


def test_a_sentence_across_the_first_border_is_found_once_by_the_next_window(stand_ins):
    at = 1490  # the quote runs past 1500: not in the triage window, whole in window 1
    monitor.tick(is_demo=True, fabric=feed(text_with(at, n=2600)))
    assert [c[1:3] for c in candidates()] == [q_at(at)]


def test_a_statement_longer_than_the_overlap_across_a_border_is_in_no_window_and_not_extracted(monkeypatch):
    """Negative: one sentence of ~600 characters (no boundary inside) from 1100 - window 0 ends at 1500,
    window 1 can only start inside it: no window holds it whole. Every window is read and nothing is
    extracted from it - read is not 'every fact extracted'."""
    long = "国家药监局" + "在本期内依据有关规定" * 58 + "共批准注册医疗器械产品318个。"
    text = text_with(1100, n=3000, sentence=long)
    spans = continuation.windows_of(text, 300)
    assert len(long) > 500 and not any(a <= 1100 and 1100 + len(long) <= b for a, b in spans)
    calls = []
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: biocheck_fake()(body))
    monkeypatch.setattr(llm, "chat_json", model(calls, quote=long[:-1]))
    ticks(feed(text), 3)
    assert candidates() == [] and plans()[0][1] == "complete"
    assert coverage()["windows_read_without_measures"] == len(spans) - 1


def test_a_repeated_sentence_is_located_by_the_window_that_read_it(stand_ins):
    """The same sentence at 100 and at 3000: triage reads the first, a later window the second - two
    candidates at their own places (searched in the whole text it would be 'in several places')."""
    base = text_with(3000)
    ticks(feed(base[:100] + SENTENCE + base[100 + len(SENTENCE) :]), 3)
    assert sorted(c[1:3] for c in candidates()) == [q_at(100), q_at(3000)]
    assert all(c[0] != "not_built" for c in candidates())


def test_triage_does_not_build_a_quote_it_cannot_have_read(monkeypatch):
    """A triage answer quoting text beyond its window is not built (quote_not_in_window)."""
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: biocheck_fake()(body))

    def beyond(settings_, stage, system, payload, model_cls, **options):
        call = {
            "stage": stage,
            "model": "fake",
            "finish_reason": "stop",
            "usage": {},
            "seconds": 0,
            "error": None,
        }
        if stage == "extract":
            items = [schemas.ExtractItem(index=w["index"]) for w in payload["windows"]]
            return schemas.Extraction(items=items), call
        return schemas.Triage(items=[schemas.TriageItem(index=1, alert="normal", measures=[measure()])]), call

    monkeypatch.setattr(llm, "chat_json", beyond)
    monitor.tick(is_demo=True, fabric=feed(text_with(3000)))
    assert [(c[0], c[3]) for c in candidates()] == [("not_built", "quote_not_in_window")]


def test_a_candidate_of_another_cleaned_artifact_is_never_reused(stand_ins):
    """A candidate of the same version made from another processor's text does not stand for this one."""
    fabric = feed(SENTENCE)
    doc = fabric.docs[0]
    with db.session() as s:
        s.add(
            db.MeasurementCandidate(
                workspace_id=monitor.settings().workspace_id,
                is_demo=True,
                source=doc["source"],
                record_id=doc["record_id"],
                version_id=doc["version_id"],
                raw_sha256=doc["raw_sha256"],
                processor_version="test.clean.v10",
                text_sha256="0" * 64,
                quote_start=SENTENCE.index(QUOTE),
                quote_end=SENTENCE.index(QUOTE) + len(QUOTE),
                measure=measure().model_dump(),
                status="checked",
                observed_at=datetime(2026, 5, 8, tzinfo=UTC),
                created_at=db.now(),
            )
        )
    monitor.tick(is_demo=True, fabric=fabric)
    assert len(candidates()) == 2  # the old artifact's candidate stays; this artifact gets its own


# ---- bounds, fairness, progress ----------------------------------------------------------------------------


def test_a_long_record_is_capped_says_what_was_not_read_and_reads_on_when_extended(stand_ins, monkeypatch):
    monkeypatch.setattr(monitor.settings(), "continuation_max_windows", 2)
    text = text_with(100, n=6000)
    spans = continuation.windows_of(text, 300)
    fabric = feed(text)
    monitor.tick(is_demo=True, fabric=fabric)
    assert plans()[0][1:3] == ("capped", "capped")
    cov = coverage()
    assert cov["model_read_chars"] == spans[2][1] and cov["unread_chars"] == 6000 - spans[2][1]
    assert cov["windows"]["capped"] == len(spans) - 3 and plans()[0][3] == spans[2][1]  # cursor kept
    with db.session() as s:
        rid = s.scalars(select(db.RecordRead.id)).one()
        assert continuation.extend(s, rid, 1) == 1
    due()
    monitor.tick(is_demo=True, fabric=fabric)
    assert plans()[0][3] == spans[3][1]  # read on from where it stopped


def test_reading_is_fair_across_records(stand_ins, monkeypatch):
    calls, _ = stand_ins
    monkeypatch.setattr(monitor.settings(), "continuation_calls_per_round", 1)
    long, short = text_with(100, n=9000), text_with(100, n=2600)
    monitor.tick(is_demo=True, fabric=feed(long, short))
    first = [p for st, p in calls if st == "extract"][0]
    sl, ss = continuation.windows_of(long, 300), continuation.windows_of(short, 300)
    # one call: the long record's window 1, the short one's window 1, then the long one's window 2
    assert [w["position"] for w in first["windows"]] == [
        f"{sl[1][0]}-{sl[1][1]} / 9000",
        f"{ss[1][0]}-{ss[1][1]} / 2600",
        f"{sl[2][0]}-{sl[2][1]} / 9000",
    ]


def test_reading_is_fair_across_sources(stand_ins, monkeypatch):
    calls, _ = stand_ins
    monkeypatch.setattr(monitor.settings(), "continuation_calls_per_round", 1)
    texts = [text_with(100, n=9000)] * 3
    fabric = feed(*texts, sources={0: "external_archive", 1: "external_archive", 2: "other_archive"})
    monkeypatch.setattr(monitor, "new_documents", lambda *a, **k: (fabric.docs, {}, False))
    monitor.tick(is_demo=True, fabric=fabric)
    first = [p for st, p in calls if st == "extract"][0]
    # the second source is not behind both records of the first
    assert [w["title"] for w in first["windows"]] == ["记录0", "记录2", "记录1", "记录2"]


def test_read_until_is_the_contiguous_front_and_a_failed_window_is_a_hole(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: biocheck_fake()(body))
    monkeypatch.setattr(llm, "chat_json", model(calls, error_for=lambda n: "timeout" if n == 1 else None))
    monkeypatch.setattr(monitor.settings(), "continuation_windows_per_call", 1)
    text = text_with(100)
    spans = continuation.windows_of(text, 300)
    monitor.tick(is_demo=True, fabric=feed(text))
    assert [w[3] for w in windows()][:3] == ["sent", "failed", "sent"]
    assert plans()[0][3] == 1500  # window 2 was read, window 1 not: the front stays at window 0
    assert coverage()["model_read_chars"] == 1500 + (spans[2][1] - spans[2][0])  # union, no hole filled


def test_a_withdrawn_permission_pauses_the_plan_and_it_reads_on_when_restored(stand_ins):
    fabric = feed(text_with(3000))
    monitor.tick(is_demo=True, fabric=fabric)
    fabric.revoked.add(fabric.docs[0]["version_id"])
    due()
    monitor.tick(is_demo=True, fabric=fabric)
    assert plans()[0][1:3] == ("paused_unreadable", "source_unreadable")
    assert windows()[3][3] == "source_unreadable" and windows()[3][4] == 0  # no attempt spent
    fabric.revoked.clear()
    ticks(fabric, 3)
    assert plans()[0][1] == "complete"


def test_a_spent_model_budget_leaves_windows_owed_without_spending_attempts(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: biocheck_fake()(body))
    monkeypatch.setattr(
        llm, "chat_json", model(calls, error_for=lambda n: "http_error:429" if n == 1 else None)
    )
    fabric = feed(text_with(3000))
    monitor.tick(is_demo=True, fabric=fabric)
    assert [w[3:] for w in windows()][1:3] == [("budget_spent", 0), ("budget_spent", 0)]
    ticks(fabric, 3)
    assert plans()[0][1] == "complete" and len(candidates()) == 1


def test_failures_are_retried_a_bounded_number_of_times(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: biocheck_fake()(body))
    monkeypatch.setattr(llm, "chat_json", model(calls, error_for=lambda n: "timeout"))
    ticks(feed(text_with(100, n=2600)), 4)
    assert windows()[1][3:] == ("failed_final", 3)
    assert plans()[0][1:3] == ("failed", "retries_spent")
    assert len([c for c in calls if c[0] == "extract"]) == 3  # bounded


def test_a_short_record_is_complete_with_its_triage_window(stand_ins):
    monitor.tick(is_demo=True, fabric=feed(SENTENCE))
    assert plans()[0][1] == "complete" and windows() == [(0, 0, len(SENTENCE), "sent", 0)]


def test_text_that_is_not_one_cleaned_artifact_is_not_planned_and_says_so(stand_ins):
    monitor.tick(is_demo=True, fabric=feed(text_with(3000), clean=False))
    assert plans()[0][1:4] == ("not_planned", "not_a_clean_artifact", 1500)
    assert coverage()["unread_chars"] == 2500


def test_windows_without_a_digit_are_skipped_only_when_configured_and_count_as_unread(stand_ins, monkeypatch):
    assert monitor.settings().continuation_skip_no_digit is False  # the default reads every window
    monkeypatch.setattr(monitor.settings(), "continuation_skip_no_digit", True)
    ticks(feed(text_with(3000)), 3)
    states = [w[3] for w in windows()]
    assert states[0] == "sent" and "skipped_no_digit" in states
    assert plans()[0][1:3] == ("capped", "skipped_no_digit")  # never "complete" with skipped text
    cov = coverage()
    assert cov["skipped_no_digit_chars"] > 0 and cov["unread_chars"] >= cov["skipped_no_digit_chars"]
    assert plans()[0][3] == 1500  # the front does not pass a skipped window


def test_attempts_are_append_only(stand_ins):
    monitor.tick(is_demo=True, fabric=feed(text_with(3000)))
    with db.session() as s:
        n = len(list(s.scalars(select(db.RecordReadAttempt))))
    assert n == 3  # window 0 by triage, windows 1 and 2


def test_context_carries_its_own_offsets_and_is_not_window_text(stand_ins):
    calls, _ = stand_ins
    text = "# 公告\n" + text_with(3000)[5:]
    monitor.tick(is_demo=True, fabric=feed(text))
    w = [p for st, p in calls if st == "extract"][0]["windows"][0]
    assert w["heading"] == {"at": 0, "text": "# 公告"}
    assert not w["text"].startswith("# 公告")


# ---- what a found value sets off ---------------------------------------------------------------------


def run_two_months(monkeypatch, at):
    """R0 (April, 318 in its triage window), R1 (May, 900 at `at`): the series and topic that follow."""
    may = SENTENCE.replace("2026年4月", "2026年5月").replace("318", "900")
    by_quote = {
        QUOTE: {},
        QUOTE.replace("318", "900"): {"value_text": "900", "period_text": "2026年5月", "period": "2026-05"},
    }
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: biocheck_fake()(body))
    monkeypatch.setattr(llm, "chat_json", model([], by_quote=by_quote))
    fabric = feed(text_with(100, n=1400))
    monitor.tick(is_demo=True, fabric=fabric)
    later = FakeFabric(
        [record("R1", "记录1", text_with(at, n=4000, sentence=may), available="2026-06-08T08:00:00+08:00")],
        clean=True,
    )
    fabric.docs += later.docs
    ticks(fabric, 4)
    with db.session() as s:
        points = [
            (m.record_id, m.value_text) for m in s.scalars(select(db.Measurement).order_by(db.Measurement.id))
        ]
        events = [
            sorted(c["kind"] for c in e.detail["changes"])
            for e in s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "series"))
        ]
        reasons = sorted({t.pending_reason or "" for t in s.scalars(select(db.Topic))})
    return points, events, reasons


def test_a_value_found_by_continuation_sets_off_what_one_found_by_triage_does(monkeypatch):
    by_triage = run_two_months(monkeypatch, 200)
    db.Base.metadata.drop_all(db.engine())
    db.Base.metadata.create_all(db.engine())
    by_continuation = run_two_months(monkeypatch, 3000)
    assert by_continuation == by_triage and len(by_triage[0]) == 2


# ---- re-extraction and the coverage endpoint (Codex COORD 647) -------------------------------------


def test_a_re_extracted_legacy_version_gets_its_reading_plan(stand_ins):
    """A point stored before checking is re-read for its measures - and its windows after the first are owed
    like any triaged version's, so an upgraded long record has no blind tail."""
    text = text_with(100)
    fabric = feed(text)
    doc = fabric.docs[0]
    with db.session() as s:
        s.add(
            db.Measurement(
                workspace_id=monitor.settings().workspace_id,
                is_demo=True,
                object="国家药监局",
                metric="批准注册医疗器械产品",
                unit="个",
                kind="flow",
                scope="",
                value=318,
                period="2026-04",
                period_start=datetime(2026, 4, 1, tzinfo=UTC),
                period_end=datetime(2026, 4, 30, tzinfo=UTC),
                status="reported",
                source=doc["source"],
                record_id=doc["record_id"],
                version_id=doc["version_id"],
                quote="318个",
                available_at=datetime(2026, 5, 8, tzinfo=UTC),
            )
        )
        s.add(  # the feed already saw it: only the re-extraction reads it
            db.Observation(
                workspace_id=monitor.settings().workspace_id,
                source=doc["source"],
                record_id=doc["record_id"],
                version_id=doc["version_id"],
                is_demo=True,
                title="legacy",
                available_at=datetime(2026, 5, 8, tzinfo=UTC),
                alert="none",
                reason="triaged before checking",
            )
        )
    monitor.tick(is_demo=True, fabric=fabric)
    assert plans() and plans()[0][1] in ("reading", "complete")
    assert [w[3] for w in windows()][:3] == ["sent", "sent", "sent"]


def test_the_coverage_endpoint_is_read_only_signed_in_and_by_workspace_and_demo_flag(stand_ins):
    from fastapi.testclient import TestClient

    from bioradar.app import app

    monitor.tick(is_demo=True, fabric=feed(text_with(3000), SENTENCE))
    client = TestClient(app)
    assert client.get("/api/measure-coverage?is_demo=true").status_code == 401
    assert (
        client.post("/api/session", json={"credential": monitor.settings().dev_credential}).status_code == 200
    )
    body = client.get("/api/measure-coverage?is_demo=true").json()
    assert body["plans_by_state"] == {"reading": 1, "complete": 1}
    long = next(i for i in body["items"] if i["total_chars"] == 4000)
    assert long["unread_by_reason"]["pending"] == long["unread_chars"] > 0
    assert long["processor_version"] and long["text_sha256"]
    assert client.get("/api/measure-coverage?is_demo=false").json()["items"] == []
    assert client.post("/api/measure-coverage").status_code == 405  # read-only


# ---- authority, isolation, leases (Codex r149) ---------------------------------------------------------


@pytest.mark.parametrize("state", ["not_processed", None, "pending", "not_visible_or_not_processed"])
def test_only_an_artifact_fabric_calls_processed_is_sent(stand_ins, state):
    """Old clean fields under any other state are never sent: the windows stay owed, no attempt spent."""
    calls, _ = stand_ins
    fabric = feed(text_with(3000))
    monitor.tick(is_demo=True, fabric=fabric)  # triage + windows 1, 2 while processed
    real = fabric.clean_texts

    def clean_texts(*a, **k):
        out = real(*a, **k)
        for entry in out.values():
            entry["state"] = state
        return out

    fabric.clean_texts = clean_texts
    sent = len([c for c in calls if c[0] == "extract"])
    due()
    monitor.tick(is_demo=True, fabric=fabric)
    assert len([c for c in calls if c[0] == "extract"]) == sent
    assert windows()[3][3:] == (
        "source_unreadable" if state == "not_visible_or_not_processed" else "pending",
        0,
    )


def test_a_plan_of_another_workspace_is_never_claimed(stand_ins):
    calls, _ = stand_ins
    fabric = feed(text_with(3000))
    monitor.tick(is_demo=True, fabric=fabric)
    with db.session() as s:
        s.execute(update(db.RecordRead).values(workspace_id="another-workspace"))
    sent = len([c for c in calls if c[0] == "extract"])
    due()
    monitor.tick(is_demo=True, fabric=fabric)
    assert len([c for c in calls if c[0] == "extract"]) == sent


def test_a_lease_that_ran_out_during_the_model_call_is_not_used(monkeypatch):
    import time

    calls = []
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: biocheck_fake()(body))
    slow = model(calls)

    def slow_extract(settings_, stage, system, payload, model_cls, **options):
        if stage == "extract":
            time.sleep(0.05)
        return slow(settings_, stage, system, payload, model_cls, **options)

    monkeypatch.setattr(llm, "chat_json", slow_extract)
    monkeypatch.setattr(continuation, "LEASE", timedelta(milliseconds=10))
    monitor.tick(is_demo=True, fabric=feed(text_with(3000)))
    assert [w[3] for w in windows()][1:3] == ["pending", "pending"]  # not taken as read
    assert candidates() == []
