"""T-26: existing state carried on (Codex 708, r167) - a version already read from one cleaned artifact is
upgraded to a new processor's artifact of the same raw version, and corrections seen before record_corrections
existed are backfilled; both bounded, continued by run id, never by a cleared cursor or a fresh database."""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from conftest import FakeFabric, record
from sqlalchemy import select
from test_continuation import QUOTE, SENTENCE, due, effective, model, text_with
from test_continuation import stand_ins as stand_ins  # noqa: F401 - the fixture

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.adapters.fabric import FabricError, part
from bioradar.material import Material
from bioradar.research import continuation, measure_checks, monitor, upkeep

NEW = "test.clean.v12"
OLD = FakeFabric.PROCESSOR
TAIL = "\n\n附：本版为重新清洗的文本。"
SOURCE = "external_archive"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class UpgradeFabric(FakeFabric):
    """FakeFabric's v11 artifacts, plus a v12 artifact of a version (self.v12) once it is `ready`: before,
    Fabric answers it queued; `down` makes the v12 reads fail as a Fabric that does not answer."""

    def __init__(self, records):
        super().__init__(records, clean=True)
        self.v12, self.ready, self.down = {}, set(), False

    def clean_texts(self, source, version_ids, *, is_demo, mode, cutoff, processor_version=None):
        if processor_version != NEW:
            return super().clean_texts(
                source,
                version_ids,
                is_demo=is_demo,
                mode=mode,
                cutoff=cutoff,
                processor_version=processor_version,
            )
        if self.down:
            raise FabricError("fabric_http_502", "down")
        out = {}
        for d in self.docs:
            v = d["version_id"]
            if d["source"] != source or v not in version_ids or v not in self.v12:
                continue
            if v not in self.ready:
                out[v] = {"state": "queued", "clean": None}
                continue
            t = self.v12[v]
            out[v] = {
                "state": "processed",
                "clean": {"processor_version": NEW, "text": t, "status": "parsed", "text_sha256": sha(t)},
            }
        return out

    def read_versions(self, source, refs, *, is_demo, cutoff=None, now=None, artifacts=None):
        found, missing = super().read_versions(source, refs, is_demo=is_demo, cutoff=cutoff, now=now)
        out = []
        for d in found:
            want = (artifacts or {}).get(d["version_id"]) or {}
            if want.get("processor_version") == NEW and d["version_id"] in self.ready:
                t = self.v12[d["version_id"]]
                d = dict(d, text=t, clean_text_state="processed")
                d["material"] = Material.part(t, part(d, "clean_text", processor_version=NEW))
            out.append(d)
        return out, missing


def tick(fabric, n=8):
    for _ in range(n):
        monitor.tick(is_demo=True, fabric=fabric)
        due()


def old_state(*texts):
    """Records read and checked from their v11 artifacts: plans complete, points effective."""
    fabric = UpgradeFabric(
        [record(f"R{i}", f"记录{i}", t, available="2026-05-08T08:00:00+08:00") for i, t in enumerate(texts)]
    )
    tick(fabric)
    with db.session() as s:
        assert {r.state for r in s.scalars(select(db.RecordRead))} == {"complete"}
        assert {r.artifact_key.split(":")[0] for r in s.scalars(select(db.RecordRead))} == {OLD}
    return fabric


def frozen(read_id):
    """What an old plan has: its row, windows, attempts and material reads - an upgrade changes none of it but
    superseded_by_id."""
    with db.session() as s:
        r = s.get(db.RecordRead, read_id)
        row = {
            c.name: getattr(r, c.name)
            for c in db.RecordRead.__table__.columns
            if c.name != "superseded_by_id"
        }
        ws = [
            (w.window_index, w.start_char, w.end_char, w.state, w.attempts)
            for w in s.scalars(select(db.RecordReadWindow).where(db.RecordReadWindow.read_id == read_id))
        ]
        attempts = [
            (a.id, a.window_index, a.outcome, a.material_read_id)
            for a in s.scalars(select(db.RecordReadAttempt).where(db.RecordReadAttempt.read_id == read_id))
        ]
        reads = [
            m.id for m in s.scalars(select(db.MaterialRead).where(db.MaterialRead.version_id == r.version_id))
        ]
        return row, ws, attempts, reads


def plans():
    with db.session() as s:
        return list(s.scalars(select(db.RecordRead).order_by(db.RecordRead.id)))


def points():
    with db.session() as s:
        return [
            (m.artifact_key.split(":")[0], m.value_text, m.retired_reason, m.retired_by, m.id)
            for m in s.scalars(select(db.Measurement).order_by(db.Measurement.id))
        ]


def events(kind):
    with db.session() as s:
        return [e.detail for e in s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == kind))]


def upgrade(fabric, run_id=None, limit=5):
    return upkeep.upgrade_reads(
        monitor.settings(), fabric, is_demo=True, source=SOURCE, processor=NEW, limit=limit, run_id=run_id
    )


def test_an_upgrade_reads_the_new_artifact_from_its_first_window_and_its_point_takes_the_old_ones_place(
    stand_ins,
):
    fabric = old_state(text_with(3000))
    [old] = plans()
    before, v = frozen(old.id), old.version_id
    fabric.v12[v] = text_with(3000) + TAIL
    # not processed yet: nothing is planned as if it were, the run says so and waits
    run = upgrade(fabric)
    assert run["items"] == {"artifact_not_ready": 1} and run["state"] == "running" and len(plans()) == 1
    fabric.ready.add(v)
    run = upgrade(fabric, run["run_id"])
    assert run["items"] == {"planned": 1}
    old, new = plans()
    assert (old.state, old.superseded_by_id, new.supersedes_id) == ("complete", new.id, old.id)
    assert new.artifact_key == db.artifact_key(NEW, sha(fabric.v12[v])) and new.read_until == 0
    with db.session() as s:
        ws = list(s.scalars(select(db.RecordReadWindow).where(db.RecordReadWindow.read_id == new.id)))
        assert ws[0].window_index == 0 and ws[0].state == "pending"  # triage read the old artifact, not this
        assert continuation.coverage(s, s.get(db.RecordRead, new.id))["model_read_chars"] == 0
    assert frozen(old.id) == before  # the old plan, windows, attempts and reads are kept as they were
    tick(fabric)
    assert plans()[1].state == "complete"
    [(a0, v0, why0, by0, id0), (a1, v1, why1, by1, id1)] = points()
    assert (a0, v0, why0, by0) == (OLD, "318", "artifact_upgrade", id1) and (a1, v1, why1) == (
        NEW,
        "318",
        None,
    )
    assert (
        effective() == ["318"] and events("record_conflict") == [] and events("artifact_value_changed") == []
    )
    with db.session() as s:
        retired = measure_checks.comparability(s, s.get(db.Measurement, id0))
        assert retired["state"] == "not_comparable" and "superseded_by_artifact" in retired["reasons"]
    assert frozen(old.id)[:3] == before[:3]
    run = upgrade(fabric, run["run_id"])
    assert run["items"] == {"settled": 1} and run["state"] == "done"


def test_a_value_the_upgraded_artifact_reads_differently_replaces_the_old_one_and_is_told(
    stand_ins, monkeypatch
):
    fabric = old_state(text_with(3000))
    [old] = plans()
    q2 = QUOTE.replace("318", "320")
    fabric.v12[old.version_id] = text_with(3000, sentence=SENTENCE.replace("318", "320")) + TAIL
    fabric.ready.add(old.version_id)
    monkeypatch.setattr(llm, "chat_json", model([], by_quote={QUOTE: {}, q2: {"value_text": "320"}}))
    run = upgrade(fabric)
    tick(fabric)
    [(a0, v0, why0, by0, _), (a1, v1, why1, _, id1)] = points()
    assert (a0, v0, why0, by0) == (OLD, "318", "artifact_upgrade", id1) and (a1, v1, why1) == (
        NEW,
        "320",
        None,
    )
    assert effective() == ["320"]
    assert (
        events("record_conflict") == []
    )  # two readings of one raw version, not the record stating two values
    [changed] = events("artifact_value_changed")
    assert [p["value"] for p in changed["points"]] == ["318", "320"] and changed["reprocessed"] is True
    with db.session() as s:  # dated when the reprocessing happened, not at the record's time (Codex 770)
        [e] = s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "artifact_value_changed"))
        assert monitor.parse_time(e.at) > datetime(2026, 11, 1, tzinfo=UTC)
        [p0, _] = s.scalars(select(db.Measurement).order_by(db.Measurement.id))
        assert monitor.parse_time(p0.retired_at) > datetime(2026, 11, 1, tzinfo=UTC)
    assert upgrade(fabric, run["run_id"])["items"] == {"settled": 1}


@pytest.mark.parametrize("present", [False, True])
def test_a_point_the_upgraded_artifact_did_not_give_again_stays_owed_not_proven_absent(
    stand_ins, monkeypatch, present
):
    """Codex r184: the new reading found nothing - whether the sentence left the new text or the model gave no
    item for it - the old point stays as it is and the run stays open, saying whether its quote is still
    there."""
    fabric = old_state(text_with(3000))
    [old] = plans()
    sentence = SENTENCE if present else "。" * len(SENTENCE)
    fabric.v12[old.version_id] = text_with(3000, sentence=sentence) + TAIL
    fabric.ready.add(old.version_id)
    run = upgrade(fabric)
    if present:  # the extractor gives no item although the sentence is there
        monkeypatch.setattr(llm, "chat_json", model([], by_quote={"不会出现的句子": {}}))
    tick(fabric)
    assert plans()[1].state == "complete"
    run = upgrade(fabric, run["run_id"])
    assert run["items"] == {"upgrade_unconfirmed": 1} and run["state"] == "running"
    [(a0, v0, why0, by0, id0)] = points()
    assert (a0, v0, why0, by0) == (OLD, "318", None, None) and effective() == ["318"]
    with db.session() as s:
        [item] = s.scalars(select(db.UpkeepItem).where(db.UpkeepItem.outcome == "upgrade_unconfirmed"))
        assert item.detail["unconfirmed"] == [
            {"id": id0, "value": "318", "quote_in_new_text": present, "recheck": None}
        ]
    assert upgrade(fabric, run["run_id"])["recorded"]["upgrade_unconfirmed"] == 1  # said once, not again


def test_a_partial_upgrade_read_on_settles_once_complete(stand_ins):
    """Codex r184: capped, then extended and read to the end - the same run settles and is done."""
    fabric = old_state(text_with(3000))
    [old] = plans()
    fabric.v12[old.version_id] = text_with(3000) + TAIL
    fabric.ready.add(old.version_id)
    run = upgrade(fabric)
    new_id = plans()[1].id
    with db.session() as s:
        for w in s.scalars(select(db.RecordReadWindow).where(db.RecordReadWindow.read_id == new_id)):
            w.state = "capped" if w.window_index else "sent"  # as Codex's probe: window 0 counted read
        continuation._settle(s, s.get(db.RecordRead, new_id))
    assert upgrade(fabric, run["run_id"])["items"] == {"upgrade_incomplete": 1}
    with db.session() as s:
        assert continuation.extend(s, new_id, 50) > 0
    tick(fabric)
    run = upgrade(fabric, run["run_id"])
    assert run["items"] == {"settled": 1} and run["state"] == "done"
    assert [p[2] for p in points()] == ["artifact_upgrade", None]


def test_a_new_plan_read_only_in_part_keeps_the_old_points_and_says_so(stand_ins, monkeypatch):
    fabric = old_state(text_with(3000))
    [old] = plans()
    fabric.v12[old.version_id] = text_with(3000) + TAIL
    fabric.ready.add(old.version_id)
    run = upgrade(fabric)
    with db.session() as s:  # every window of the new plan beyond the first capped
        for w in s.scalars(select(db.RecordReadWindow).where(db.RecordReadWindow.read_id == plans()[1].id)):
            w.state = "capped" if w.window_index else "sent"
        continuation._settle(s, s.get(db.RecordRead, plans()[1].id))
    run = upgrade(fabric, run["run_id"])
    assert run["items"] == {"upgrade_incomplete": 1} and run["state"] == "running"
    assert points()[0][2] is None and effective() == ["318"]


def test_an_upgrade_run_is_bounded_and_carries_on_after_a_restart_and_a_fabric_that_did_not_answer(stand_ins):
    fabric = old_state(text_with(3000), text_with(2500))
    first, second = plans()
    for p, t in ((first, text_with(3000)), (second, text_with(2500))):
        fabric.v12[p.version_id] = t + TAIL
        fabric.ready.add(p.version_id)
    run = upgrade(fabric, limit=1)
    assert run["items"] == {"planned": 1} and run["cursor"] == first.id
    fabric.down = True  # a fresh call (as after a restart) meets a Fabric that does not answer
    run = upgrade(fabric, run["run_id"], limit=1)
    assert run["cursor"] == first.id and run["items"] == {"planned": 1, "fabric_unavailable": 1}
    fabric.down = False
    run = upgrade(fabric, run["run_id"], limit=1)
    assert run["cursor"] == second.id and run["items"] == {"planned": 2}
    assert [p.supersedes_id for p in plans()] == [None, None, first.id, second.id]
    run = upgrade(fabric, run["run_id"], limit=1)  # nothing planned twice
    assert len(plans()) == 4


def test_corrections_seen_before_record_corrections_existed_are_backfilled_in_bounded_steps(
    stand_ins, monkeypatch
):
    fabric = UpgradeFabric(
        [
            record("A1", "通报", SENTENCE, available="2026-05-08T00:00:00+00:00"),
            record(
                "C1",
                "更正",
                "关于此前通报的更正说明，详见正文。",
                available="2026-05-10T00:00:00+00:00",
                relations=[("corrects", "A1")],
            ),
        ]
    )
    original = monitor.record_corrections
    monkeypatch.setattr(monitor, "record_corrections", lambda *a, **k: 0)  # the radar before migration 0010
    tick(fabric, 3)
    monkeypatch.setattr(monitor, "record_corrections", original)
    with db.session() as s:
        assert s.scalars(select(db.RecordCorrection)).all() == []
        [m] = s.scalars(select(db.Measurement))
        assert "withdrawn_by_correction" not in measure_checks.comparability(s, m)["reasons"]
    s_ = monitor.settings()
    run = upkeep.backfill_corrections(s_, fabric, is_demo=True, limit=1)
    assert run["items"] == {"no_correction": 1} and run["state"] == "running"
    fabric.read_versions = lambda *a, **k: (_ for _ in ()).throw(FabricError("fabric_http_502", "down"))
    run = upkeep.backfill_corrections(s_, fabric, is_demo=True, limit=1, run_id=run["run_id"])
    assert run["items"] == {"no_correction": 1, "fabric_unavailable": 1}
    del fabric.read_versions
    run = upkeep.backfill_corrections(s_, fabric, is_demo=True, limit=1, run_id=run["run_id"])
    assert run["items"] == {"no_correction": 1, "recorded": 1}
    run = upkeep.backfill_corrections(s_, fabric, is_demo=True, limit=1, run_id=run["run_id"])
    assert run["state"] == "done"
    with db.session() as s:
        [c] = s.scalars(select(db.RecordCorrection))
        assert (c.target_record_id, c.by_record_id, c.relation) == ("A1", "C1", "corrects")
        assert monitor.parse_time(c.visible_at) == datetime.fromisoformat("2026-05-10T00:00:00+00:00")
        [m] = s.scalars(select(db.Measurement))
        assert "withdrawn_by_correction" in measure_checks.comparability(s, m)["reasons"]
        before = datetime.fromisoformat("2026-05-09T00:00:00+00:00")
        assert "withdrawn_by_correction" not in measure_checks.comparability(s, m, at=before)["reasons"]
        assert "source_corrected" in (s.get(db.Topic, m.domain_id).pending_reason or "")
    again = upkeep.backfill_corrections(
        s_, fabric, is_demo=True, limit=5
    )  # a second run records nothing twice
    assert again["items"] == {"no_correction": 1, "already_recorded": 1}
    with db.session() as s:
        assert len(s.scalars(select(db.RecordCorrection)).all()) == 1


def test_a_run_is_continued_only_as_what_it_is():
    with pytest.raises(ValueError):
        upkeep.backfill_corrections(monitor.settings(), FakeFabric([]), is_demo=True, run_id=999)


# ---- working off upgrade_unconfirmed (Codex 772) ----------------------------------------------------------


def unconfirmed(monkeypatch, *texts, new=None):
    """Old state, upgraded; the new artifact read whole by an extractor that gives nothing: owed points."""
    fabric = old_state(*texts)
    for p, t in zip(plans(), texts):
        fabric.v12[p.version_id] = (new or (lambda x: x))(t) + TAIL
        fabric.ready.add(p.version_id)
    run = upgrade(fabric)
    monkeypatch.setattr(llm, "chat_json", model([], by_quote={"不会出现的句子": {}}))
    tick(fabric)
    run = upgrade(fabric, run["run_id"])
    assert run["items"] == {"upgrade_unconfirmed": len(texts)}
    return fabric, run


def recheck(fabric, run, limit=5):
    return upkeep.recheck_unconfirmed(
        monitor.settings(), fabric, is_demo=True, run_id=run["run_id"], limit=limit
    )


def test_a_known_fact_still_in_the_new_text_is_relocated_checked_anew_and_takes_the_old_ones_place(
    stand_ins, monkeypatch
):
    calls, checks = stand_ins
    fabric, run = unconfirmed(monkeypatch, text_with(3000))
    [old] = points()
    sent_before = len(checks)
    run = recheck(fabric, run)
    assert run["items"] == {"upgrade_unconfirmed": 1, "relocated": 1}
    with db.session() as s:
        [c] = s.scalars(
            select(db.MeasurementCandidate).where(db.MeasurementCandidate.relocated_from.is_not(None))
        )
        assert (c.processor_version, c.material_read_id) == (NEW, None)  # no model call read it
        assert fabric.v12[c.version_id][c.quote_start : c.quote_end] == QUOTE
    tick(fabric)
    # a new BioCheck check, of the text Fabric serves for the new artifact
    assert any(NEW in json.dumps(b["subject"]) for b in checks[sent_before:])
    with db.session() as s:
        check = s.scalars(
            select(db.MeasurementCheck)
            .where(db.MeasurementCheck.candidate_id == c.id)
            .order_by(db.MeasurementCheck.id)
        ).all()[-1]
        assert check.status == "consistent" and check.gate == "series" and check.request_sha256
    [(a0, v0, why0, by0, _), (a1, v1, why1, _, id1)] = points()
    assert (a0, why0, by0) == (OLD, "artifact_upgrade", id1) and (a1, v1, why1) == (NEW, "318", None)
    assert effective() == ["318"]
    run = upgrade(fabric, run["run_id"])
    assert run["state"] == "done" and run["items"]["settled"] == 1


def test_a_recheck_the_check_does_not_pass_leaves_the_old_point_owed(stand_ins, monkeypatch):
    fabric, run = unconfirmed(monkeypatch, text_with(3000))
    recheck(fabric, run)

    def inconsistent(body, response):
        if NEW not in json.dumps(body["subject"]):
            return response  # only the recheck in the new artifact does not pass
        for it in response["items"]:
            it.update(status="inconsistent", fields={**it["fields"], "value": {"result": "mismatch"}})
        return response

    from conftest import biocheck_fake

    post = biocheck_fake(inconsistent)
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: post(body))
    tick(fabric)
    [(a0, _, why0, _, id0)] = points()
    assert (a0, why0) == (OLD, None) and effective() == ["318"]
    run = upgrade(fabric, run["run_id"])
    assert run["state"] == "running"
    with db.session() as s:
        item = s.scalars(
            select(db.UpkeepItem)
            .where(db.UpkeepItem.outcome == "upgrade_unconfirmed")
            .order_by(db.UpkeepItem.id.desc())
        ).first()
        [owed] = item.detail["unconfirmed"]
        assert owed["id"] == id0 and owed["recheck"]["check"]["status"] == "inconsistent"


@pytest.mark.parametrize(
    "new,outcome",
    [
        (lambda t: t.replace(SENTENCE, "。" * len(SENTENCE)), "quote_not_in_new_text"),
        (lambda t: t + "\n\n" + SENTENCE, "quote_in_several_places"),
    ],
)
def test_a_quote_gone_or_found_twice_is_not_rechecked_and_stays_owed(stand_ins, monkeypatch, new, outcome):
    fabric, run = unconfirmed(monkeypatch, text_with(3000), new=new)
    run = recheck(fabric, run)
    assert run["items"] == {"upgrade_unconfirmed": 1, outcome: 1}
    assert recheck(fabric, run)["recorded"][outcome] == 1  # the artifact is fixed: not tried again
    with db.session() as s:
        assert (
            s.scalars(
                select(db.MeasurementCandidate).where(db.MeasurementCandidate.relocated_from.is_not(None))
            ).all()
            == []
        )
    assert points()[0][2] is None and upgrade(fabric, run["run_id"])["state"] == "running"


def test_a_fact_citing_a_context_only_its_old_model_call_was_sent_is_not_rechecked(stand_ins, monkeypatch):
    fabric, run = unconfirmed(monkeypatch, text_with(3000))
    with db.session() as s:
        [c] = s.scalars(select(db.MeasurementCandidate))
        c.measure = {**c.measure, "sources": {"unit": "heading"}}
    run = recheck(fabric, run)
    assert run["items"] == {"upgrade_unconfirmed": 1, "cites_old_send": 1}
    assert points()[0][2] is None


def test_rechecks_are_bounded_and_carry_on_after_a_restart_and_an_unreadable_artifact(stand_ins, monkeypatch):
    fabric, run = unconfirmed(monkeypatch, text_with(3000), text_with(2500))
    real = fabric.clean_texts
    fabric.clean_texts = lambda *a, **k: (_ for _ in ()).throw(FabricError("fabric_http_502", "down"))
    run = recheck(fabric, run, limit=1)
    assert run["items"] == {"upgrade_unconfirmed": 2, "recheck_waiting": 1}
    fabric.clean_texts = real
    run = recheck(fabric, run, limit=1)  # a fresh call, as after a restart
    assert run["items"] == {"upgrade_unconfirmed": 2, "relocated": 1}
    run = recheck(fabric, run, limit=1)
    assert run["items"] == {"upgrade_unconfirmed": 2, "relocated": 2}
    assert recheck(fabric, run, limit=5)["recorded"]["relocated"] == 2  # nothing relocated twice


def test_a_relocated_fact_takes_the_place_of_its_own_old_point_whatever_its_group_key_was(
    stand_ins, monkeypatch
):
    """Found on the real old-state copy: an old point from before the complete domain (no family, no
    dims_key) is taken over by the point of the candidate relocated from it, not looked up by group key."""
    fabric, run = unconfirmed(monkeypatch, text_with(3000))
    with db.session() as s:
        [m] = s.scalars(select(db.Measurement))
        m.family, m.dims_key = "", ""
    recheck(fabric, run)
    tick(fabric)
    [(a0, _, why0, by0, _), (a1, _, why1, _, id1)] = points()
    assert (a0, why0, by0) == (OLD, "artifact_upgrade", id1) and (a1, why1) == (NEW, None)
    assert upgrade(fabric, run["run_id"])["state"] == "done"
