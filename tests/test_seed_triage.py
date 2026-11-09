"""The records an analysis starts from (research/run/seeds.py): the plan reads what they say, the monitor's
triage judges whether they are related at all, and a run whose seeds are unrelated or too thin ends completed
with that verdict as its stop - not failed, and not made up into a report. No real model: a stand-in."""

import pytest
from conftest import FakeFabric, record
from sqlalchemy import select
from test_groups import day, observation

from bioradar import db
from bioradar.adapters import llm
from bioradar.adapters.fabric import FabricError
from bioradar.config import settings
from bioradar.research import analyses, groups, schemas
from bioradar.research.pipeline import Run
from bioradar.research.run import Failed

# the lead the first judgment of D1 was made about: a news index's headline, no body
HORMUZ = "Trump Insists On Strait Of Hormuz Blockade, Claims Nothing Is Going To Iran, No Oil Going There"
WHO = "WHO reports Ebola outbreak in Equateur province"
WHO_TEXT = "Health authorities confirmed 7 cases in Equateur province."


def stand_in(sent, *, related=lambda title: "related", triage=True, plan=True):
    """A model answering the seed triage (a verdict by title) and the plan (one check about the seed, one
    search); triage=False / plan=False: that call fails (no answer)."""

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        sent.append((stage, payload))
        call = {"stage": stage, "service": "radar", "model": "fake", "finish_reason": "stop", "usage": {}}
        call |= {"seconds": 0.1, "error": None}
        if stage == "seed_triage" and triage:
            items = [
                schemas.TriageItem(
                    index=r["index"],
                    direction="public_health",
                    alert="none",
                    related=related(r["title"]),
                    reason="理由：" + r["title"][:12],
                )
                for r in payload["records"]
            ]
            return schemas.Triage(items=items), call
        if stage == "plan" and plan:
            checks = [schemas.PlanCheck(text="病例是否属实", required=True, objects=[1])]
            queries = [schemas.PlanQuery(terms=["Ebola"], checks=[1])]
            objects = [schemas.PlanObject(name="Equateur")]
            return schemas.Plan(objects=objects, checks=checks, queries=queries), call
        return None, call | {"error": "http_503"}

    return chat_json


def start(source, record_id, version):
    return {"source": source, "record_id": record_id, "version_id": version}


def new_analysis(seeds, question="这条消息是否属实？"):
    sources = sorted({s["source"] for s in seeds})
    return analyses.create(
        question=question,
        direction="public_health",
        is_demo=True,
        sources=sources,
        enqueue=False,
        seed_records=seeds,
    )


def run_all(monkeypatch, row, fabric):
    """analyses.execute, with this Fabric: what a worker does with the queued analysis."""
    monkeypatch.setattr(analyses, "Run", lambda row_, s, lease_token=None: Run(row_, s, fabric, lease_token))
    analyses.execute(row.id)
    return analyses.get(row.id)


def stages(sent):
    return [stage for stage, _ in sent]


def test_an_unrelated_lead_ends_completed_with_the_verdict_and_is_neither_asked_nor_selected_again(
    monkeypatch,
):
    sent = []
    monkeypatch.setattr(llm, "chat_json", stand_in(sent, related=lambda title: "unrelated"))
    lead = record("G1", HORMUZ, "", source="gdelt_doc", kind="news_report", version="L1")
    lead_row = observation("L1", "G1", day(3), None, source="gdelt_doc")
    lead_row.reason = "news_index_lead"  # as the monitor of before the triage of leads left it
    with db.session() as session:
        session.add_all([lead_row, db.Cursor(source="gdelt_doc", is_demo=True, after_seq=1)])
    fabric = FakeFabric([lead])

    done = run_all(monkeypatch, new_analysis([start("gdelt_doc", "G1", "L1")]), fabric)

    # completed, not failed: the first judgment of the lead is "not about public health", with the reason
    assert (done.state, done.error_code) == ("completed", None)
    assert stages(sent) == ["seed_triage"]  # the plan was not asked: nothing to plan
    assert done.result["stop"]["decision"] == "unrelated"
    assert HORMUZ[:20] in done.result["stop"]["reason"] and "理由：" in done.result["stop"]["reason"]
    assert (done.result["report_status"], done.result["publishable"]) == ("NOT_RUN", False)
    [seen] = done.progress["seed_triage"]
    assert (seen["related"], seen["stored"], seen["dropped"]) == ("unrelated", False, True)
    # the verdict is kept on the observation: the groups' view leaves the lead out, asking again costs nothing
    with db.session() as session:
        assert session.scalar(select(db.Observation.relevance)) == "unrelated"
        picked = groups.for_selection(session, settings(), is_demo=True, cutoff=day(4), background=set())
    assert picked["selection"]["reason"] == "no_ranked_group"
    again = run_all(monkeypatch, new_analysis([start("gdelt_doc", "G1", "L1")], "同一条线索再问一次"), fabric)
    assert again.result["stop"]["decision"] == "unrelated" and stages(sent) == ["seed_triage"]
    assert done.progress["seed_triage"][0]["stored"] is False and again.progress["seed_triage"][0]["stored"]


def test_a_related_seed_reaches_the_plan_with_its_title_and_text_and_its_names_count_as_the_question(
    monkeypatch,
):
    sent = []
    monkeypatch.setattr(llm, "chat_json", stand_in(sent))
    fabric = FakeFabric([record("D1", WHO, WHO_TEXT, source="who_don", version="V1")])
    run = Run(new_analysis([start("who_don", "D1", "V1")]), settings(), fabric)

    assert run.plan() is False  # not ended: the run goes on to investigate

    assert stages(sent) == ["seed_triage", "plan"]
    plan_payload = sent[1][1]
    [seed] = plan_payload["seeds"]
    assert (seed["title"], seed["text"]) == (WHO, WHO_TEXT)
    assert seed["triage"]["related"] == "related" and "理由" in seed["triage"]["reason"]
    assert [h["record_id"] for h in run.p["hits"]] == ["D1"]
    # "Equateur" is in the seed, not in the question: still an object of it, not an exploratory guess
    assert run.p["objects"] == ["Equateur"] and run.p["plan_basis"]["Equateur"] == "question"
    with db.session() as session:
        rows = session.scalars(select(db.MaterialRead).order_by(db.MaterialRead.id))
        reads = [(r.stage, r.ref, r.status) for r in rows]
    assert reads == [("seed_triage", "S1", "sent"), ("plan", "S1", "sent")]  # what each model got is recorded


def test_a_seed_too_thin_to_plan_from_ends_completed_and_a_plan_call_that_fails_still_fails(monkeypatch):
    sent = []
    thin = FakeFabric([record("D1", WHO, "", source="who_don", version="V1")])
    seeds = [start("who_don", "D1", "V1")]

    base = stand_in(sent)

    def nothing_to_ask(settings_, stage, system, payload, model_cls, **options):
        """The model finds nothing it can ask about the title: a plan with no checks."""
        value, call = base(settings_, stage, system, payload, model_cls, **options)
        return (schemas.Plan(checks=[], queries=[]) if stage == "plan" else value), call

    monkeypatch.setattr(llm, "chat_json", nothing_to_ask)
    result = Run(new_analysis(seeds), settings(), thin).run()

    assert result["stop"]["decision"] == "insufficient"
    assert WHO in result["stop"]["reason"] and "正文0字" in result["stop"]["reason"]  # what there is, said

    monkeypatch.setattr(llm, "chat_json", stand_in([], plan=False))  # the plan call itself fails
    with pytest.raises(Failed) as failed:
        Run(new_analysis(seeds, "另一个问题"), settings(), thin).run()
    assert failed.value.code == "plan_failed"


def test_a_seed_fabric_says_is_not_there_is_left_out_but_a_failing_fabric_fails_the_run(monkeypatch):
    sent = []
    monkeypatch.setattr(llm, "chat_json", stand_in(sent))
    seeds = [start("who_don", "D1", "V1")]

    run = Run(new_analysis(seeds), settings(), FakeFabric([]))  # the version is not visible to this reader
    assert run.plan() is False  # noted, not a seed: the plan goes on
    [note] = [n for n in run.p["notes"] if n["code"] == "seed_unreadable"]
    assert "record_not_visible" in note["detail"] and "seed_triage" not in run.p

    class Down(FakeFabric):
        code = "fabric_http_503"

        def read_versions(self, *args, **kwargs):
            raise FabricError(self.code, "unavailable")

    sent.clear()
    for code in (
        "fabric_http_503",
        "version_identity_mismatch",
    ):  # a 5xx; a version that is not the one named
        down = Down([record("D1", WHO, WHO_TEXT, source="who_don", version="V1")])
        down.code = code
        with pytest.raises(Failed) as failed:
            Run(new_analysis(seeds, code), settings(), down).run()
        assert failed.value.code == "seed_unreadable"
    assert sent == []  # not passed on as "no seeds" to a plan


def test_a_failed_triage_is_no_verdict_and_a_seed_that_corrects_is_read_whatever_the_model_says(monkeypatch):
    sent = []
    monkeypatch.setattr(llm, "chat_json", stand_in(sent, triage=False))
    fabric = FakeFabric([record("D1", WHO, WHO_TEXT, source="who_don", version="V1")])
    run = Run(new_analysis([start("who_don", "D1", "V1")]), settings(), fabric)

    assert run.plan() is False
    assert run.p["seed_triage"] == [] and any(n["code"] == "seed_triage_failed" for n in run.p["notes"])
    assert sent[1][1]["seeds"][0]["triage"] is None  # the plan is told nothing was judged
    assert [h["record_id"] for h in run.p["hits"]] == ["D1"]  # a failure is never "unrelated"

    sent.clear()
    monkeypatch.setattr(llm, "chat_json", stand_in(sent, related=lambda title: "unrelated"))
    notice = record(
        "C1",
        "Correction notice",
        "Correction: 6 cases, not 7.",
        source="who_don",
        version="V2",
        relations=[("corrects", "D1")],
    )
    run = Run(new_analysis([start("who_don", "C1", "V2")]), settings(), FakeFabric([notice]))

    assert run.plan() is False  # judged unrelated, and still read: a correction is never skipped for that
    assert [t["related"] for t in run.p["seed_triage"]] == ["unrelated"]
    assert [h["record_id"] for h in run.p["hits"]] == ["C1"]
