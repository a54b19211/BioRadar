"""One analysis end to end with fake Fabric, model and services."""

import re

from conftest import FakeFabric, record
from fabric_receipts import sources_receipt
from sqlalchemy import select

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.config import settings
from bioradar.research import analyses, schemas
from bioradar.research.pipeline import Run
from bioradar.research.run import assess, investigate, read, search, submit

RECORDS = [
    record(
        "B31",
        "XQ-17 随机试验初步结果",
        "随机试验纳入240例，PFS的HR为0.62。",
        available="2026-09-29T08:00:00+08:00",
    ),
    record(
        "B41",
        "XQ-17 试验结果更正",
        "更正：同一试验PFS的HR为0.69，95%CI 0.51-0.93。",
        available="2026-09-30T08:00:00+08:00",
        relations=[("corrects", "B31")],
    ),
    record("N1", "无关新闻", "某公司搬迁。"),
]


def fake_model(sent):
    def chat_json(settings_, stage, system, payload, model_cls, **options):
        sent.append((stage, payload, options))
        if stage == "plan":
            value = schemas.Plan(
                checks=[schemas.PlanCheck(text="当前PFS疗效数值", required=True)],
                queries=[schemas.PlanQuery(terms=["PFS"], checks=[1])],
                judgments=["是否为重要进展"],
                needs_calculation=True,
            )
        elif stage == "read":
            text = payload["material"]["text"]
            quote = "HR为0.69" if "更正" in text else "HR为0.62"
            value = schemas.Reading(
                relevant=True,
                facts=[
                    schemas.ReadFact(text="PFS风险比", quote=quote, checks=["Q1"], stance="supports"),
                    schemas.ReadFact(text="编造的事实", quote="OS显著延长", checks=["Q1"], stance="supports"),
                ],
                answered=["Q1"],
            )
        elif stage == "calculate":
            fact = next(f for f in payload["facts"] if "0.69" in f["quote"])
            value = schemas.Calculations(
                calculations=[
                    schemas.Calculation(
                        label="风险降低比例",
                        operands=[schemas.Operand(fact=fact["id"], value=0.69)],
                        expression="(1 - x1) * 100",
                        unit="%",
                    )
                ]
            )
        elif stage == "seed_triage":  # the seeds of an analysis are all medical news here
            value = schemas.Triage(
                items=[
                    schemas.TriageItem(
                        index=r["index"], direction="public_health", alert="normal", related="related"
                    )
                    for r in payload["records"]
                ]
            )
        else:
            value = schemas.Assessment(
                alert="important",
                judgments=[
                    schemas.Judgment(
                        question="是否为重要进展",
                        conclusion="有限人群内的重要进展",
                        confidence="probable",
                        credibility="high",
                        consequence="high",
                        priority="high",
                        grounds=["E1", "E9"],
                        unproven="还缺总生存期（OS）数据，这是最能区分的下一项证据",
                    )
                ],
            )
        return value, {
            "stage": stage,
            "service": "radar",
            "model": "fake",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "seconds": 0.1,
            "error": None,
        }

    return chat_json


def first_policy(body):
    """Stand-in for Bio-Planner: search first, then read in order, stop when nothing is left."""
    for i, c in enumerate(body["candidates"]):
        if c["kind"] in {"search", "read"}:
            return {"decision": "continue", "candidate": i, "reason": "test"}
    return {"decision": "complete", "candidate": None, "reason": "test"}


def test_analysis_end_to_end(monkeypatch):
    sent, biocheck = [], []
    monkeypatch.setattr(llm, "chat_json", fake_model(sent))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))

    def fake_biocheck(s, operation, body):
        biocheck.append((operation, body))
        call = [{"stage": operation, "model": "fake", "usage": {"total_tokens": 7}}]
        if operation == "generate":
            call[0]["requests"] = 2  # a format retry inside BioCheck
            return {"draft": "同一试验更正后PFS的HR为0.69[E1]。", "llm_calls": call}
        return {"status": "PASS", "claims": [], "coverage": {"verdict": "complete"}, "llm_calls": call}

    monkeypatch.setattr(services, "biocheck", fake_biocheck)
    row = analyses.create(
        question="XQ-17 的PFS结果现在应怎样判断？", direction="pipeline", is_demo=True, enqueue=False
    )
    result = Run(row, settings(), fabric=FakeFabric(RECORDS)).run()

    with db.session() as s:
        evidence = list(s.scalars(select(db.Evidence).order_by(db.Evidence.ref)))
    # the planner reads the newer of two equally matching records first, so the correction is E1
    assert [(e.ref, e.record_id) for e in evidence] == [("E1", "B41"), ("E2", "B31")]
    assert result["report_status"] == "PASS"
    # The invented quote is dropped, the real ones stay.
    assert [n["code"] for n in result["notes"]].count("quote_not_found") == 2
    assert result["checks"][0]["status"] == "answered" and result["checks"][0]["evidence"] == ["E1", "E2"]
    # The correction reaches BioCheck as a source update, old -> new.
    generate = biocheck[0][1]
    assert generate["source_updates"] == [{"target": "E2", "replacement": "E1", "kind": "correction"}]
    assert {e["source_kind"] for e in generate["evidence"]} == {"official"}
    # The number is computed by the program, not by the model.
    assert result["computations"][0]["value"] == 31.0
    assert generate["analysis_results"][0]["id"] == "A1"
    # Judgment grounds keep only refs the verified report actually cites.
    assert result["assessment"]["judgments"][0]["grounds"] == ["E1"]
    # The conclusion itself goes back to BioCheck; with no supported sentence it is capped.
    assert biocheck[-1][1]["draft"] == "有限人群内的重要进展[E1]。"
    judgment = result["assessment"]["judgments"][0]
    assert (judgment["check"], judgment["confidence"]) == ("unverified", "possible")
    # Structured stages run with thinking off (the default of chat_json).
    assert all(not options.get("thinking") for _, _, options in sent)
    assert (
        result["usage"]["llm_calls"] == 1 + 2 + 1 + 1 + 3
    )  # plan, two reads, calculate, assess, BioCheck x3 (generate, verify, judgments)
    assert result["usage"]["requests"] == result["usage"]["llm_calls"] + 1
    # v3 6.1: what the reader and BioCheck received is recorded per record with its binding, and reads
    # back from the parts it names (here each record's fixed-version text in the fake Fabric)
    from bioradar.material import part_key, reread

    texts = {d["version_id"]: d["text"] for d in FakeFabric(RECORDS).docs}
    with db.session() as s:
        reads = list(s.scalars(select(db.MaterialRead).order_by(db.MaterialRead.id)))
    assert {(r.stage.split(":")[0], r.service) for r in reads} == {("read", "radar"), ("report", "biocheck")}
    by_ref = {(r.stage, r.ref): r for r in reads}
    for e in evidence:
        assert e.meta["material_read_id"] == by_ref[("read", e.ref)].id
        assert by_ref[("read", e.ref)].sha256 == __import__("hashlib").sha256(e.text.encode()).hexdigest()
    sent_texts = {e["id"]: e["text"] for e in generate["evidence"]}
    # every record was read once; each BioCheck request that carried its report window is its own record
    # (write, check, then the check of the conclusions), all sent
    assert {(r.stage, r.attempt, r.status) for r in reads} == {
        ("read", 1, "sent"),
        ("report:generate", 1, "sent"),
        ("report:verify", 1, "sent"),
        ("report:verify", 2, "sent"),
    }
    for r in reads:
        resolve = lambda parts: {part_key(q): texts[q["version_id"]] for q in parts}  # noqa: E731
        text = reread(r.binding, resolve)
        assert text == (sent_texts[r.ref] if r.stage == "report" else evidence[int(r.ref[1:]) - 1].text)


def test_planner_unavailable_fails_cleanly(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", fake_model([]))

    def down(s, body):
        raise services.ServiceError("planner", "unreachable")

    monkeypatch.setattr(services, "decide", down)
    row = analyses.create(question="任意问题示例", direction="science", is_demo=True, enqueue=False)
    try:
        Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == "planner_unreachable"
    else:
        raise AssertionError("expected failure")


def test_same_question_returns_existing_analysis():
    first = analyses.create(question="同一个问题示例", direction="science", is_demo=True, enqueue=False)
    again = analyses.create(
        question="同一个问题示例 ",
        direction="science",
        is_demo=True,
        enqueue=False,
        investigation_id=first.investigation_id,
    )
    assert again.id == first.id


def empty_report(s, operation, body):
    if operation == "generate":
        return {"draft": "无。", "llm_calls": []}
    return {"status": "REVIEW", "claims": [], "coverage": {"verdict": "incomplete"}, "llm_calls": []}


def test_scope_limits_search_and_reads_to_visible_records(monkeypatch):
    """A fixed material set whose terms match nothing still gets read; later and outside records do not."""
    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    row = analyses.create(
        question="当前对异常聚集能作什么判断？",
        direction="public_health",
        is_demo=True,
        cutoff="2026-09-29T12:00:00+08:00",
        scope_record_ids=["B31", "B41"],
        enqueue=False,
    )
    fabric = FakeFabric(RECORDS)
    Run(row, settings(), fabric=fabric).run()
    with db.session() as s:
        read = [e.record_id for e in s.scalars(select(db.Evidence))]
    assert read == ["B31"]  # B41 became visible after the cutoff; N1 is outside the set
    assert all(call[3] == ("B31", "B41") for call in fabric.calls if call[0] == "search")


def test_an_empty_and_search_also_retries_without_its_first_term(monkeypatch):
    base = fake_model([])

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        value, call = base(settings_, stage, system, payload, model_cls, **options)
        if stage == "plan":
            value.queries = [schemas.PlanQuery(terms=["细胞治疗", "HR"], checks=[1])]
        return value, call

    monkeypatch.setattr(llm, "chat_json", chat_json)
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    row = analyses.create(question="HR现在是多少？", direction="pipeline", is_demo=True, enqueue=False)
    fabric = FakeFabric(RECORDS)
    Run(row, settings(), fabric=fabric).run()
    assert ("HR",) in [call[2] for call in fabric.calls if call[0] == "search"]
    with db.session() as s:
        assert {e.record_id for e in s.scalars(select(db.Evidence))} == {"B31", "B41"}


def test_a_large_scope_is_filtered_locally(monkeypatch):
    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    scope = ["B31"] + [f"X{i}" for i in range(200)]
    row = analyses.create(
        question="PFS现在是多少？", direction="pipeline", is_demo=True, scope_record_ids=scope, enqueue=False
    )
    fabric = FakeFabric(RECORDS)
    Run(row, settings(), fabric=fabric).run()
    # term searches are filtered here; only browsing the set passes its ids
    assert all(call[3] == () for call in fabric.calls if call[0] == "search" and call[2])
    with db.session() as s:
        assert [e.record_id for e in s.scalars(select(db.Evidence))] == ["B31"]  # B41 is outside the set


def test_retries_of_empty_searches_are_capped(monkeypatch):
    from bioradar.research.run.search import MAX_RETRIES

    base = fake_model([])

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        value, call = base(settings_, stage, system, payload, model_cls, **options)
        if stage == "plan":
            value.queries = [schemas.PlanQuery(terms=[f"无{i}", f"空{i}"], checks=[1]) for i in range(8)]
        return value, call

    monkeypatch.setattr(llm, "chat_json", chat_json)
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    row = analyses.create(question="什么也搜不到的问题", direction="pipeline", is_demo=True, enqueue=False)
    fabric = FakeFabric(RECORDS)
    Run(row, settings(), fabric=fabric).run()
    assert len([c for c in fabric.calls if c[0] == "search"]) == 8 + MAX_RETRIES


def test_a_check_with_supporting_facts_stays_open_until_the_reader_says_it_is_whole(monkeypatch):
    base = fake_model([])

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        value, call = base(settings_, stage, system, payload, model_cls, **options)
        if stage == "read":
            value.answered = []
        return value, call

    monkeypatch.setattr(llm, "chat_json", chat_json)
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    row = analyses.create(question="三家公司各自的PFS？", direction="pipeline", is_demo=True, enqueue=False)
    result = Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    assert result["checks"][0]["status"] == "open" and result["checks"][0]["evidence"] == ["E1", "E2"]


def test_facts_that_only_say_what_a_record_does_not_mention_do_not_answer_a_check(monkeypatch):
    """Two routine weekly reports said "未提及更正" / "未提及独立来源"; the reader called every
    required check answered and the planner stopped after two reads with 40 event records unread."""
    base = fake_model([])

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        value, call = base(settings_, stage, system, payload, model_cls, **options)
        if stage == "read":
            for fact in value.facts:
                fact.stance = "limits"
            value.answered = ["Q1"]
        return value, call

    monkeypatch.setattr(llm, "chat_json", chat_json)
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    row = analyses.create(question="XQ-17 的PFS？", direction="pipeline", is_demo=True, enqueue=False)
    result = Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    assert result["checks"][0]["status"] == "open" and result["checks"][0]["evidence"]


def test_scenario_material_is_judged_inside_the_scenario_and_real_questions_stay_as_asked(monkeypatch):
    """The materials say they are invented; the models concluded "fictional, so nothing to verify"."""
    from bioradar.research.pipeline import DEMO_SETTING

    for is_demo in (True, False):
        sent, bodies = [], []
        monkeypatch.setattr(llm, "chat_json", fake_model(sent))
        monkeypatch.setattr(services, "decide", lambda s, body: bodies.append(body) or first_policy(body))
        monkeypatch.setattr(
            services, "biocheck", lambda s, op, body: bodies.append(body) or empty_report(s, op, body)
        )
        records = [dict(r, is_demo=is_demo) for r in RECORDS]
        row = analyses.create(
            question="XQ-17 的PFS？",
            direction="pipeline",
            is_demo=is_demo,
            sources=["external_archive"],
            enqueue=False,
        )
        Run(row, settings(), fabric=FakeFabric(records)).run()
        questions = [payload["question"] for _, payload, _ in sent if "question" in payload]
        questions += [body["question"] for body in bodies if "question" in body]
        assert questions and all(q == "XQ-17 的PFS？" + (DEMO_SETTING if is_demo else "") for q in questions)
        assert row.input["question"] == "XQ-17 的PFS？"


def test_a_next_page_in_current_mode_is_read_at_the_time_of_its_first_page(monkeypatch):
    """Live Europe PMC answered invalid_cursor twice; each page was asked "as of now" again."""
    sent = []
    monkeypatch.setattr(llm, "chat_json", fake_model(sent))
    monkeypatch.setattr(services, "biocheck", empty_report)
    order = iter(["search", "next_page"])

    def decide(s, body):
        want = next(order, None)
        pick = next((i for i, c in enumerate(body["candidates"]) if c["kind"] == want), None)
        if pick is None:
            return {"decision": "complete", "candidate": None, "reason": "test"}
        return {"decision": "continue", "candidate": pick, "reason": "test"}

    monkeypatch.setattr(services, "decide", decide)
    records = [record(f"P{i}", f"PFS 研究 {i}", "PFS 的结果") for i in range(15)]
    fabric = FakeFabric(records)
    row = analyses.create(question="XQ-17 的PFS？", direction="pipeline", is_demo=True, enqueue=False)
    Run(row, settings(), fabric=fabric).run()
    first, second = fabric.times[0], fabric.times[1]
    assert first[1] is None and second[1] is not None  # the second call is the next page
    assert first[2] is not None and second[2] == first[2]


def test_without_fabrics_admin_token_there_is_no_fetch_step_and_the_note_says_so(monkeypatch):
    """An empty admin token went out as "Bearer " and failed as LocalProtocolError."""
    sent = []
    monkeypatch.setattr(llm, "chat_json", fake_model(sent))
    kinds = []

    def decide(s, body):
        kinds.extend(c["kind"] for c in body["candidates"])
        return first_policy(body)

    monkeypatch.setattr(services, "decide", decide)
    monkeypatch.setattr(services, "biocheck", empty_report)
    for token, fetch in (("", False), ("admin-token", True)):
        kinds.clear()
        s = settings().model_copy(update={"fabric_admin_token": token})
        row = analyses.create(
            question=f"XQ-17 的PFS？{token}", direction="pipeline", sources=["clinicaltrials"], enqueue=False
        )
        result = Run(row, s, fabric=FakeFabric([], readable={"clinicaltrials"})).run()
        assert ("fetch" in kinds) is fetch
        notes = [n["code"] for n in result["notes"]]
        assert notes.count("fetch_not_configured") == (0 if fetch else 1)


def test_every_draft_and_its_check_are_kept_so_a_wrong_citation_can_be_traced(monkeypatch):
    """Only revised=true survived, so nobody could tell which step mis-cited."""
    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    checks = iter(["REVIEW", "PASS"])

    def biocheck(s, op, body):
        if op == "generate":
            return {"draft": "HR为0.62[E2]。", "llm_calls": []}
        if op == "revise":
            return {"draft": "HR为0.62[E1]。", "changed": [1], "llm_calls": []}
        status = next(checks, "PASS")  # later verify calls check the judgments
        claim = {
            "index": 1,
            "start": 0,
            "end": 11,
            "citations": [body["draft"][-4:-2]],
            "labels": [],
            "verdict": "supported" if status == "PASS" else "contradicted",
        }
        return {"status": status, "claims": [claim], "coverage": {"verdict": "complete"}, "llm_calls": []}

    monkeypatch.setattr(services, "biocheck", biocheck)
    row = analyses.create(question="XQ-17 的PFS？", direction="pipeline", is_demo=True, enqueue=False)
    result = Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    versions = result["review"]["versions"]
    assert [(v["step"], v["status"], v["claims"][0]["citations"]) for v in versions] == [
        ("generate", "REVIEW", ["E2"]),
        ("revise_1", "PASS", ["E1"]),
    ]
    assert versions[0]["draft"] == "HR为0.62[E2]。" and result["draft"] == "HR为0.62[E1]。"


def test_a_draft_whose_check_failed_is_kept_unchecked_with_the_failure(monkeypatch):
    """A version was kept only after verify returned, so a failed check lost that draft."""
    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))

    def biocheck(s, op, body):
        if op == "generate":
            return {"draft": "HR为0.62[E1]。", "llm_calls": []}
        raise services.ServiceError("biocheck", "http_502", "down")

    monkeypatch.setattr(services, "biocheck", biocheck)
    row = analyses.create(question="XQ-17 的PFS？", direction="pipeline", is_demo=True, enqueue=False)
    result = Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    assert result["review"]["versions"] == [
        {"step": "generate", "draft": "HR为0.62[E1]。", "status": "unchecked", "error": "http_502"}
    ]


def test_an_object_named_by_the_record_itself_is_kept_and_followed_up(monkeypatch):
    """An open question names no place, so every place found in the records was blanked and
    events had nothing to hang on. Plan-stage guesses still never get in this way."""
    from bioradar.research.schemas import ReadFact

    row = analyses.create(
        question="当前有哪些异常聚集？", direction="public_health", is_demo=True, enqueue=False
    )
    run = Run(row, settings(), fabric=FakeFabric([]))
    run.p.update(objects=[], queries=[], actions=[], checks=[])
    named = ReadFact(
        text="禾屿报告3例", quote="禾屿县本周报告3例发热伴皮疹", stance="supports", object="禾屿县"
    )
    guessed = ReadFact(text="病例增加", quote="本周报告3例", stance="supports", object="澜津市")
    assert read.object_of(run, named, "E1") == "禾屿县" and read.object_of(run, guessed, "E2") == ""
    assert run.p["found_objects"] == [{"name": "禾屿县", "ref": "E1", "quote": "禾屿县本周报告3例发热伴皮疹"}]
    assert run.p["objects"] == []  # found objects do not narrow the question
    # A known name still needs this fact's own quote, or two events get joined
    other = ReadFact(text="乙城医院收治2例", quote="乙城医院收治2例", stance="supports", object="禾屿县")
    assert read.object_of(run, other, "E3") == ""
    # background keeps its name but takes no place among the names followed up
    background = ReadFact(
        text="甲城门诊量平稳", quote="甲城门诊本周登记10人", stance="context", object="甲城"
    )
    assert read.object_of(run, background, "E4") == "甲城"
    assert [o["name"] for o in run.p["found_objects"]] == ["禾屿县"]


def test_a_full_query_pool_lets_evidence_replace_background_terms_not_yet_run():
    """Two background-only terms from the first record took the last free places; terms from
    the event records found later never got in, so the run depended on which record came first."""
    row = analyses.create(
        question="当前有哪些异常聚集？", direction="public_health", is_demo=True, enqueue=False
    )
    run = Run(row, settings(), fabric=FakeFabric([]))
    plan = [{"terms": [f"泛词{i}"], "checks": ["Q1"]} for i in range(10)]
    run.p.update(
        queries=list(plan),
        actions=[{"kind": "search", "args": {"terms": ["泛词0"]}}],
        pages={
            "s|泛词0": {"cursor": "c", "pages": 1, "new": True, "found": 10, "checks": ["Q1"]}
        },  # still paging
        notes=[],
    )
    search.add_query(run, ["芦川发热门诊"], ["Q1"], value=0, origin="read:E1")
    search.add_query(run, ["登记方法"], ["Q1"], value=0, origin="read:E1")
    assert len(run.p["queries"]) == 12
    search.add_query(run, ["澜津"], ["Q1"], value=3, origin="object:E5")
    search.add_query(run, ["SIM-RX-26"], ["Q1"], value=2, origin="read:E5")
    terms = [q["terms"][0] for q in run.p["queries"]]
    assert "澜津" in terms and "SIM-RX-26" in terms
    assert "芦川发热门诊" not in terms and "登记方法" not in terms and "泛词0" in terms  # run searches stay
    search.add_query(
        run, ["另一背景词"], ["Q1"], value=0, origin="read:E6"
    )  # worth no more than what is there
    assert "另一背景词" not in [q["terms"][0] for q in run.p["queries"]]
    assert [n["code"] for n in run.p["notes"]].count("query_replaced") == 2


def test_searches_run_and_used_up_leave_the_active_pool_so_new_evidence_gets_in():
    """Once all 12 searches had run, a new term from new evidence could never get in."""
    row = analyses.create(
        question="当前有哪些异常聚集？", direction="public_health", is_demo=True, enqueue=False
    )
    run = Run(row, settings(), fabric=FakeFabric([]))
    queries = [{"terms": [f"词{i}"], "checks": ["Q1"]} for i in range(12)]
    run.p.update(
        queries=list(queries),
        actions=[
            {"kind": "search", "source": "external_archive", "args": {"terms": q["terms"]}} for q in queries
        ],
        pages={
            "external_archive|词3": {"cursor": "c", "pages": 1, "new": True, "found": 10, "checks": ["Q1"]}
        },
        notes=[],
    )
    search.add_query(
        run, ["背景新词"], ["Q1"], value=0, origin="read:E8"
    )  # background never retires a search
    assert "背景新词" not in [q["terms"][0] for q in run.p["queries"]]
    search.add_query(run, ["新城"], ["Q1"], value=3, origin="object:E9")
    terms = [q["terms"][0] for q in run.p["queries"]]
    assert "新城" in terms and "词0" not in terms and "词3" in terms  # still paging, so it stays
    assert run.p["notes"][-1]["code"] == "query_retired"
    assert {"kind": "search", "source": "external_archive", "args": {"terms": ["词0"]}} in run.p["actions"]
    # With two sources, a search run on one only is not done
    two = Run(
        analyses.create(
            question="两源？", direction="public_health", is_demo=True, sources=["a", "b"], enqueue=False
        ),
        settings(),
        fabric=FakeFabric([]),
    )
    two.p.update(
        queries=[{"terms": [f"词{i}"], "checks": ["Q1"]} for i in range(12)],
        actions=[{"kind": "search", "source": "a", "args": {"terms": ["词0"]}}],
        pages={},
        notes=[],
        hits=[],
        objects=[],
    )
    search.add_query(two, ["新城"], ["Q1"], value=3, origin="object:E9")
    assert "词0" in [q["terms"][0] for q in two.p["queries"]]  # b has not searched it yet
    assert any(c.get("follows_up") for c in investigate.candidates(two) if c["args"].get("terms") == ["新城"])


def test_a_percentage_of_different_things_is_not_computed(monkeypatch):
    """2 devices over 3 institutions came out as 66.6667%; a caveat does not make it valid."""
    from bioradar.research import pipeline

    def calculate(settings_, stage, system, payload, model_cls, **options):
        ids = [f["id"] for f in payload["facts"]]
        calcs = [
            schemas.Calculation(
                label="设备覆盖比例",
                operands=[
                    schemas.Operand(fact=ids[0], value=2, unit="套"),
                    schemas.Operand(fact=ids[1], value=3, unit="家"),
                ],
                expression="x1 / x2 * 100",
                unit="%",
            ),
            schemas.Calculation(
                label="阳性比例",
                operands=[
                    schemas.Operand(fact=ids[2], value=5, unit="人"),
                    schemas.Operand(fact=ids[3], value=20, unit="人"),
                ],
                expression="x1 / x2 * 100",
                unit="%",
            ),
        ]
        return schemas.Calculations(calculations=calcs), {"stage": stage, "error": None}

    monkeypatch.setattr(llm, "chat_json", calculate)
    row = analyses.create(question="覆盖情况如何？", direction="business", is_demo=True, enqueue=False)
    run = Run(row, settings(), fabric=FakeFabric([]))
    quotes = ["采购2套设备", "合作3家机构", "阳性5人", "检测20人"]
    run.p.update(
        needs_calculation=True,
        relations=[],
        notes=[],
        facts=[{"id": f"F{i + 1}", "ref": "E1", "text": q, "quote": q} for i, q in enumerate(quotes)],
    )
    monkeypatch.setattr(pipeline.Run, "log", lambda self, calls, service="radar": None)
    run.compute()
    assert [c["label"] for c in run.p["computations"]] == ["阳性比例"]
    assert any(n["code"] == "calculation_mixed_units" for n in run.p["notes"])


def test_a_percentage_whose_units_cannot_be_read_from_the_quotes_is_not_computed(monkeypatch):
    """With no units, or "人" claimed for both though the quotes say 套 and 家, 66.67% came out."""
    from bioradar.research import pipeline

    claimed = {}

    def calculate(settings_, stage, system, payload, model_cls, **options):
        ids = [f["id"] for f in payload["facts"]]
        a, b, c = claimed["units"]
        calcs = [
            schemas.Calculation(
                label="覆盖比例",
                operands=[
                    schemas.Operand(fact=ids[0], value=2, unit=a),
                    schemas.Operand(fact=ids[1], value=3, unit=b),
                ],
                expression="x1 / x2 * 100",
                unit="%",
            ),
            schemas.Calculation(
                label="每套金额",
                operands=[
                    schemas.Operand(fact=ids[2], value=120, unit=c),
                    schemas.Operand(fact=ids[0], value=2, unit="套"),
                ],
                expression="x1 / x2",
                unit="万元/套",
            ),
        ]
        return schemas.Calculations(calculations=calcs), {"stage": stage, "error": None}

    monkeypatch.setattr(llm, "chat_json", calculate)
    monkeypatch.setattr(pipeline.Run, "log", lambda self, calls, service="radar": None)
    for units, code in (
        (("", "", "万元"), "calculation_unit_unverified"),
        (("人", "人", "万元"), "calculation_unit_unverified"),
    ):
        claimed["units"] = units
        row = analyses.create(
            question=f"覆盖情况如何？{units}", direction="business", is_demo=True, enqueue=False
        )
        run = Run(row, settings(), fabric=FakeFabric([]))
        quotes = ["采购2套设备", "合作3家机构", "合同总额120万元"]
        run.p.update(
            needs_calculation=True,
            relations=[],
            notes=[],
            facts=[{"id": f"F{i + 1}", "ref": "E1", "text": q, "quote": q} for i, q in enumerate(quotes)],
        )
        run.compute()
        assert [c["label"] for c in run.p["computations"]] == ["每套金额"]  # 120万元/2套 is no percentage
        assert run.p["computations"][0]["value"] == 60
        assert any(n["code"] == code for n in run.p["notes"])


def test_reads_tell_the_planner_what_they_found_apart_from_answering(monkeypatch):
    """A read that found a key fact without finishing a broad check reported gain 0 only."""
    base = fake_model([])
    bodies = []

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        value, call = base(settings_, stage, system, payload, model_cls, **options)
        if stage == "read":
            value.answered = []  # nothing is finished
        return value, call

    monkeypatch.setattr(llm, "chat_json", chat_json)
    monkeypatch.setattr(services, "decide", lambda s, body: bodies.append(body) or first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    row = analyses.create(question="XQ-17 的PFS？", direction="pipeline", is_demo=True, enqueue=False)
    Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    reads = [h for h in bodies[-1]["history"] if h["kind"] == "read"]
    assert reads and all(h["gain"] == 0.0 for h in reads)
    assert any(h["evidence_gain"] >= 0.6 for h in reads)  # decisive facts were found
    assert all("evidence_gain" not in h for h in bodies[-1]["history"] if h["kind"] == "search")


def test_the_report_input_keeps_every_quoted_fact_even_far_down_a_long_record():
    """A trial's status fields filled the 4000 characters with context, the loop stopped, and
    the results table the reader had quoted never reached BioCheck."""
    from bioradar.material import Material
    from bioradar.research.run.windows import excerpt

    quotes = [f"字段{i}的值是{i * 7}" for i in range(12)]
    text = "".join("背景" * 400 + q for q in quotes)  # each quote 800 characters after the last
    sent = excerpt(Material.literal(text), quotes, limit=4000).text
    assert all(q in sent for q in quotes) and len(sent) <= 4000 + 12 * 3


def test_a_queued_follow_up_checks_its_earlier_report_again_when_it_runs():
    """Permission is checked when the run uses the earlier report, not only at creation."""
    parent = analyses.create(question="上一轮？", direction="pipeline", is_demo=True, enqueue=False)
    with db.session() as s:
        row = s.get(db.Analysis, parent.id)
        row.result = {"draft": "上一轮稿件[E1]。"}
        s.add(
            db.Evidence(
                analysis_id=parent.id,
                ref="E1",
                source="gone",
                record_id="r",
                version_id="v",
                title="t",
                text="x",
                meta={},
            )
        )
    child = analyses.create(
        question="追问？", direction="pipeline", is_demo=True, parent_id=parent.id, enqueue=False
    )
    assert child.input["previous_report"]["draft"] == "上一轮稿件[E1]。"

    class Listed(FakeFabric):
        def __init__(self, listed):
            super().__init__([])
            self.listed = listed

        def sources(self):
            return sources_receipt(self.listed)

    assert Run(child, settings(), fabric=Listed(["gone"])).previous_report()["draft"] == "上一轮稿件[E1]。"
    run = Run(child, settings(), fabric=Listed(["external_archive"]))
    assert run.previous_report() is None and run.p["notes"][-1]["code"] == "previous_report_withheld"
    assert Run(child, settings(), fabric=FakeFabric([])).previous_report() is None  # list unreadable


def test_candidates_carry_what_they_are_expected_to_add():
    """A plain search had impact 1 while reads had 0.4-0.7, so open questions kept searching."""
    row = analyses.create(question="当前有哪些事件？", direction="public_health", is_demo=True, enqueue=False)
    run = Run(row, settings(), fabric=FakeFabric([]))
    hits = [
        {
            "source": "external_archive",
            "record_id": r,
            "title": "",
            "checks": ["Q1"],
            "critical": False,
            "read": False,
        }
        for r in ("e1", "e2", "e3", "bg")
    ]
    run.docs = {
        ("external_archive", r): {"title": "", "text": f"病例聚集{r}", "published_at": None}
        for r in ("e1", "e2", "e3")
    }
    run.docs[("external_archive", "bg")] = {"title": "", "text": "门诊登记说明", "published_at": None}
    run.p.update(
        queries=[
            {"terms": ["病例"], "checks": ["Q1"], "origin": "plan", "value": 1},
            {"terms": ["澜津市"], "checks": ["Q1"], "origin": "object:E3", "value": 3},
            {"terms": ["背景词"], "checks": ["Q1"], "origin": "read:E1", "value": 0},
        ],
        hits=hits,
        actions=[],
        pages={},
        objects=[],
        notes=[],
    )
    impact = {
        (c["kind"], tuple(c["args"].get("terms", ())) or c["args"].get("record_id")): c.get("impact")
        for c in investigate.candidates(run)
    }
    assert impact[("search", ("澜津市",))] == 1.0
    assert abs(impact[("search", ("病例",))] - 0.18) < 1e-9  # three usable records wait
    assert abs(impact[("search", ("背景词",))] - 0.06) < 1e-9  # background terms, and records wait
    assert impact[("read", "e1")] == 1.0 and impact[("read", "bg")] == 0.5
    assert all("_value" not in c and "_copy" not in c for c in investigate.candidates(run))


def test_the_earlier_report_is_checked_at_each_use_and_a_long_or_looping_chain_is_unknown():
    """Revoked between plan and report, the cached answer still sent the report to the writer;
    round 21 of a chain lost its root's limits; a loop must not hang."""
    from bioradar.research.lineage import lineage, lineage_sources

    class Listed(FakeFabric):
        def __init__(self, listed):
            super().__init__([])
            self.listed = listed

        def sources(self):
            return sources_receipt(self.listed)

    root = analyses.create(question="根？", direction="pipeline", is_demo=True, enqueue=False)
    with db.session() as s:
        s.get(db.Analysis, root.id).result = {"draft": "根稿[E1]。"}
        s.add(
            db.Evidence(
                analysis_id=root.id,
                ref="E1",
                source="gone",
                record_id="r",
                version_id="v",
                title="t",
                text="x",
                meta={},
            )
        )
    child = analyses.create(
        question="追问？", direction="pipeline", is_demo=True, parent_id=root.id, enqueue=False
    )
    fabric = Listed(["gone"])
    run = Run(child, settings(), fabric=fabric)
    assert run.previous_report() is not None
    fabric.listed = []  # revoked during the run
    assert run.previous_report() is None
    # 25 rounds: only the root cites anything; the walk reaches it
    last = child
    for i in range(23):
        last = analyses.create(
            question=f"第{i}轮？", direction="pipeline", is_demo=True, parent_id=last.id, enqueue=False
        )
    assert lineage_sources(last.id) == {"gone"}
    with db.session() as s:  # a loop is not followed forever and is not taken as "nothing upstream"
        s.get(db.Analysis, root.id).parent_id = last.id
    assert lineage_sources(last.id) is None
    with db.session() as s:
        import pytest

        from bioradar.research.lineage import UnknownLineage

        with pytest.raises(UnknownLineage):
            lineage(s, last.id)


def test_a_large_fixed_material_set_can_be_browsed_so_discovery_does_not_hang_on_a_term():
    """Within the task's own material set, list what is visible at the cutoff."""
    early = [
        record(f"S{i:03d}", f"记录{i}", "门诊登记", available="2026-09-20T08:00:00+08:00") for i in range(110)
    ]
    late = [record(f"L{i}", f"后来{i}", "后来的", available="2026-10-20T08:00:00+08:00") for i in range(5)]
    scope = [r["source_record_id"] for r in early + late]
    row = analyses.create(
        question="当前有哪些值得核实的事件？",
        direction="public_health",
        is_demo=True,
        cutoff="2026-10-01T00:00:00+08:00",
        scope_record_ids=scope,
        enqueue=False,
    )
    fabric = FakeFabric(early + late)
    times = []
    plain = fabric.search

    def timed(*args, **kwargs):
        times.append(kwargs["now"])
        return plain(*args, **kwargs)

    fabric.search = timed
    run = Run(row, settings(), fabric=fabric)
    run.p.update(
        queries=[],
        hits=[],
        actions=[],
        pages={},
        objects=[],
        notes=[],
        checks=[{"id": "Q1", "text": "事件", "required": True, "status": "open", "evidence": []}],
    )
    first = [c for c in investigate.candidates(run) if c["args"].get("browse")]
    assert len(first) == 1 and first[0]["args"]["page"] == 0 and first[0]["args"]["terms"] == []
    investigate.act(run, first[0])  # one page: one Fabric request, 100 ids
    assert run.p["actions"][-1]["found"] == 100 and len(run.p["hits"]) == 100
    from bioradar.adapters.fabric import FabricError

    def down(*args, **kwargs):
        times.append(kwargs["now"])
        raise FabricError("fabric_http_503", "down")

    real, fabric.search = fabric.search, down
    second = [c for c in investigate.candidates(run) if c["args"].get("browse")]
    assert second[0]["args"]["page"] == 1
    investigate.act(run, second[0])
    assert run.p["actions"][-1]["status"] == "failed"
    fabric.search = real
    import json

    resumed = Run(row, settings(), fabric=fabric)  # a restart keeps what was browsed and what failed
    resumed.p = json.loads(json.dumps(run.p))
    again = [c for c in investigate.candidates(resumed) if c["args"].get("browse")]
    assert again and again[0]["args"]["page"] == 1  # the failed page is offered again
    investigate.act(resumed, again[0])
    assert len(resumed.p["hits"]) == 110  # 10 more were visible; the 5 later ones were not
    assert not [c for c in investigate.candidates(resumed) if c["args"].get("browse")]
    assert [n["code"] for n in resumed.p["notes"]].count("scope_browsed") == 2
    # one material set, one moment: page 2, its retry and the resumed run all read at the first page's time
    assert len(times) == 3 and len(set(times)) == 1 and times[0] is not None
    small = analyses.create(
        question="小材料集？",
        direction="public_health",
        is_demo=True,
        scope_record_ids=scope[:3],
        enqueue=False,
    )
    run = Run(small, settings(), fabric=fabric)
    run.p.update(queries=[], hits=[], actions=[], pages={}, objects=[], notes=[], checks=[])
    assert not [
        c for c in investigate.candidates(run) if c["args"].get("browse")
    ]  # small sets are listed at plan


def test_a_registry_records_essentials_reach_the_writer_even_when_no_fact_quoted_them():
    """The reader never quoted the phase, so the writer wrote that the phase was not given. The
    record is Fabric's cleaned trial text (v3 3.3): field headings "<名称> <field>:", nested lines as paths.
    Every window of the report input keeps its range in that text (v3 6.1)."""
    from bioradar.material import Material, part_key, reread
    from bioradar.research.run.windows import excerpt, report_text

    lines = [
        "Brief summary.",
        "分期 phase: PHASE3",
        "状态 status: COMPLETED",
        "入组人数 enrollment: 31",
        "入选标准 eligibility:",
        "eligibilityCriteria: " + "条件" * 2000,
        "结果-终点测量 results_outcome_measures:",
    ]
    for i in range(1, 13):
        lines += [
            f"outcomeMeasures[{i}] title: 终点{i}; type: PRIMARY",
            f"outcomeMeasures[{i}].classes[1].categories[1].measurements[1] groupId: OG000; value: {i}0"
            + "；组B 1" * 60,
        ]
    lines += ["outcomeMeasures[13] title: 次要终点; type: SECONDARY", "主要申办方 lead_sponsor: name: X"]
    text = "\n".join(lines)
    part = {
        "kind": "clean_text",
        "source": "clinicaltrials",
        "record_id": "NCT1",
        "version_id": "v" * 64,
        "is_demo": True,
        "processor_version": "datafabric.clean_text.v11",
    }
    record = Material.part(text, part)
    sent = report_text(record, ["Brief summary."])
    head = sent.text.split("\n…\n")[0]
    assert "分期 phase: PHASE3" in head and "状态 status: COMPLETED" in head
    assert "入组人数 enrollment: 31" in head
    assert "outcomeMeasures[1] title: 终点1; type: PRIMARY" in head and "value: 10" in head
    assert "SECONDARY" not in head
    assert "写稿输入放不下、未提供：" in head  # the rest is named, not silently cut
    assert "Brief summary." in sent.text and "条件条件条件" not in sent.text[:200]
    # every character of the report input is either radar text or a bound range of the cleaned record
    binding = sent.binding()
    assert {s["part"]["kind"] for s in binding["segments"] if "part" in s} == {"clean_text"}
    assert reread(binding, lambda parts: {part_key(p): text for p in parts}) == sent.text
    plain = Material.literal("普通材料" * 2000)
    assert report_text(plain, ["普通材料"]).text == excerpt(plain, ["普通材料"]).text


def test_a_run_that_used_an_earlier_report_stops_when_its_sources_are_revoked(monkeypatch):
    """After the plan turned the earlier report into checks, revoking it only dropped the
    previous_report field; the checks still reached the writer through research_scope."""
    from bioradar.research.pipeline import Failed

    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    parent = analyses.create(question="上一轮？", direction="pipeline", is_demo=True, enqueue=False)
    with db.session() as s:
        s.get(db.Analysis, parent.id).result = {"draft": "上一轮稿件[E1]。"}
        s.add(
            db.Evidence(
                analysis_id=parent.id,
                ref="E1",
                source="gone",
                record_id="r",
                version_id="v",
                title="t",
                text="x",
                meta={},
            )
        )
    child = analyses.create(
        question="XQ-17 的PFS？", direction="pipeline", is_demo=True, parent_id=parent.id, enqueue=False
    )

    class Listed(FakeFabric):
        listed = ["gone", "external_archive"]

        def sources(self):
            return sources_receipt(self.listed)

    fabric = Listed(RECORDS)
    run = Run(child, settings(), fabric=fabric)
    run.plan()
    run.save("investigate")
    assert run.p["used_previous"]
    fabric.listed = ["external_archive"]  # revoked after the plan
    import pytest

    with pytest.raises(Failed) as stopped:
        run.run()
    assert stopped.value.code == "upstream_access_withdrawn"
    # Revoked inside the investigation, after the stage started: the next Fabric action stops
    fabric.listed = ["gone", "external_archive"]
    inside = Run(analyses.get(child.id), settings(), fabric=fabric)
    inside.stage = "investigate"
    candidate = next(c for c in investigate.candidates(inside) if c["kind"] == "search")
    fabric.listed = ["external_archive"]
    calls_before = len(fabric.calls)
    with pytest.raises(Failed):
        investigate.act(inside, candidate)
    assert len(fabric.calls) == calls_before  # nothing went to Fabric
    # progress saved before the field existed: an earlier report and past the plan counts as used
    old = Run(analyses.get(child.id), settings(), fabric=fabric)
    old.stage = "report"
    old.p.pop("used_previous", None)
    with pytest.raises(Failed):
        old.report_input()
    # Revoked while BioCheck generated the draft: verify/revise are not sent with the same scope
    fabric.listed = ["gone", "external_archive"]
    writer = Run(analyses.get(child.id), settings(), fabric=fabric)
    writer.stage = "report"
    sent = []

    def biocheck(s, operation, body):
        sent.append(operation)
        if operation == "generate":
            fabric.listed = ["external_archive"]
        return empty_report(s, operation, body)

    monkeypatch.setattr(services, "biocheck", biocheck)
    body = writer.report_input()
    writer.report_input = lambda: dict(body, evidence=[{"ref": "E1"}])
    with pytest.raises(Failed) as stopped:
        writer.report()
    assert stopped.value.code == "upstream_access_withdrawn" and sent == ["generate"]


def test_excerpt_finds_a_quote_whose_spacing_differs_from_the_source():
    from bioradar.material import Material
    from bioradar.research.run.windows import excerpt

    text = "甲" * 5000 + "PFS的HR\n为 0.69" + "乙" * 5000
    assert "0.69" in excerpt(Material.literal(text), ["PFS的HR为0.69"], limit=2000).text


def test_a_check_about_two_objects_is_not_answered_by_facts_about_one(monkeypatch):
    """R8a: three companies asked, one read — the check stays open; the planner gets only its contract."""
    base = fake_model([])
    requests = []

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        value, call = base(settings_, stage, system, payload, model_cls, **options)
        if stage == "plan":
            value.objects = [schemas.PlanObject(name="XQ-17", kind="drug"), schemas.PlanObject(name="YB-2")]
            value.checks[0].objects = [1, 2]
        if stage == "read":
            value.answered = ["Q1"]  # the reader claims it is whole
            for fact in value.facts:
                fact.object = "XQ-17"  # but every fact is about one object
        return value, call

    def decide(s, body):
        requests.append(body)
        return first_policy(body)

    monkeypatch.setattr(llm, "chat_json", chat_json)
    monkeypatch.setattr(services, "decide", decide)
    monkeypatch.setattr(services, "biocheck", empty_report)
    row = analyses.create(question="XQ-17 和 YB-2 的PFS？", direction="pipeline", is_demo=True, enqueue=False)
    result = Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    assert result["checks"][0]["status"] == "open"
    from bioradar.research.run.investigate import PLANNER_CHECK

    assert all(set(c) == set(PLANNER_CHECK) for body in requests for c in body["checks"])


def test_hypotheses_go_through_biocheck_and_an_unverified_report_cannot_confirm(monkeypatch):
    from datetime import datetime

    base = fake_model([])
    sent = []

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        value, call = base(settings_, stage, system, payload, model_cls, **options)
        if stage == "plan":
            value.hypotheses = [
                schemas.PlanHypothesis(
                    statement="待验证：XQ-17 准备申报",
                    alternatives=["只是数据更正"],
                    confirm=["上市申请受理"],
                    refute=["试验终止"],
                )
            ]
        if stage == "read":
            for fact in value.facts:
                fact.hypothesis = "H1"
        if stage == "assess":
            value.hypotheses = [
                schemas.HypothesisLevel(id="H1", level="confirmed"),
                schemas.HypothesisLevel(id="H7", level="possible"),
            ]
        return value, call

    def biocheck(s, operation, body):
        sent.append((operation, body))
        if operation == "generate":
            return {"draft": "H1 待验证的假设：XQ-17 准备申报，依据为更正后的HR[E2]。", "llm_calls": []}
        return {"status": "REVIEW", "claims": [], "coverage": {"verdict": "incomplete"}, "llm_calls": []}

    monkeypatch.setattr(llm, "chat_json", chat_json)
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", biocheck)
    at = datetime.fromisoformat("2026-09-30T08:00:00+08:00")
    with db.session() as s:
        topic = db.Topic(
            workspace_id="w",
            title="XQ-17",
            direction="pipeline",
            is_demo=True,
            first_seen_at=at,
            last_activity_at=at,
        )
        s.add(topic)
    row = analyses.create(
        question="XQ-17 信号研判",
        direction="pipeline",
        is_demo=True,
        trigger="topic",
        topic_id=topic.id,
        enqueue=False,
    )
    result = Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    generate = next(body for op, body in sent if op == "generate")
    assert generate["hypotheses"][0]["id"] == "H1" and generate["hypotheses"][0]["facts"]
    # no sentence was verified, so the tagged facts back nothing
    assert [(h["id"], h["level"]) for h in result["assessment"]["hypotheses"]] == [("H1", "possible")]
    with db.session() as s:
        watch = s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "watch")).one()
    assert [(w["id"], w["direction"]) for w in watch.detail["items"]] == [("W1", "confirm"), ("W2", "refute")]


def line_verifier(verdict_of):
    """Fake BioCheck verify for the post-report check: one claim per line, verdict_of(line)."""

    def biocheck(s, operation, body):
        claims, at = [], 0
        for line in body["draft"].split("\n"):
            claims.append({"start": at, "verdict": verdict_of(line), "citations": re.findall(r"E\d+", line)})
            at += len(line) + 1
        return {"status": "REVIEW", "claims": claims, "llm_calls": []}

    return biocheck


def test_hypothesis_grades_are_capped_by_verified_facts(monkeypatch):
    """Nothing is confirmed after the report; probable needs a supported sentence in the
    hypothesis's own paragraph and a reason BioCheck supports; unwritten hypotheses are not graded."""
    sentences = [
        ("公司于1980年成立[E5]。\n", "supported", ["E5"]),
        ("## 情报假设\n", "no_claim", []),
        ("H1 待验证的假设：工厂为 XQ-17 备货，厂房已开工[E1]。\n", "supported", ["E1"]),
        ("H2 待验证的假设：扩招为备货，招聘启事提到无菌灌装[E2]。\n", "insufficient", ["E2"]),
        ("H3 待验证的假设：年内投产，规划写明投产[E1]。", "supported", ["E1"]),
        ("另一记录显示已停建[E3]。\n", "supported", ["E3"]),
        ("H4 待验证的假设：只是合同生产，目前没有材料。\n", "no_claim", []),
        ("H6 待验证的假设：为海外订单备货。\n", "no_claim", []),
        ("小结：以上均待后续信号检验[E5]。", "supported", ["E5"]),
    ]
    draft, claims = "", []
    for text, verdict, citations in sentences:
        claims.append({"start": len(draft), "verdict": verdict, "citations": citations})
        draft += text
    row = analyses.create(
        question="工厂是否为 XQ-17 备货？", direction="business", is_demo=True, enqueue=False
    )
    run = Run(row, settings(), fabric=FakeFabric([]))
    run.p.update(
        checks=[],
        judgments=[],
        hypotheses=[{"id": h, "statement": h} for h in ("H1", "H2", "H3", "H4", "H5", "H6")],
        facts=[
            {"ref": "E1", "hypothesis": "H1", "stance": "supports"},
            {"ref": "E5", "hypothesis": "H2", "stance": "supports"},  # verified, but not about H2
            {"ref": "E2", "hypothesis": "H2", "stance": "supports"},  # about H2, not verified
            {"ref": "E1", "hypothesis": "H3", "stance": "supports"},
            {"ref": "E3", "hypothesis": "H3", "stance": "refutes"},
            {"ref": "E5", "hypothesis": "H6", "stance": "supports"},  # only in the closing line
        ],
        report={"status": "PASS", "draft": draft, "claims": claims},
    )
    graded = schemas.Assessment(
        alert="important",
        hypotheses=[
            schemas.HypothesisLevel(id=h, level="confirmed", reason="厂房已开工")
            for h in ("H1", "H2", "H3", "H5")
        ]
        + [
            schemas.HypothesisLevel(id="H4", level="refuted", reason="厂房已开工"),
            schemas.HypothesisLevel(id="H6", level="possible", reason="公司已为该药备产"),
        ],
    )
    monkeypatch.setattr(run, "ask", lambda *a, **k: graded)
    monkeypatch.setattr(run, "report_input", lambda: {"evidence": []})
    monkeypatch.setattr(
        services, "biocheck", line_verifier(lambda line: "contradicted" if "备产" in line else "supported")
    )
    run.assess()
    hypotheses = run.p["assessment"]["hypotheses"]
    assert [(h["id"], h["level"], h["grounds"]) for h in hypotheses] == [
        ("H1", "probable", ["E1"]),
        ("H2", "possible", []),
        ("H3", "possible", ["E1"]),
        ("H4", "possible", []),
        ("H6", "possible", []),
    ]
    assert "已核验报告" in hypotheses[0]["reason"]
    assert hypotheses[-1]["check"] == "contradicted" and "撤下" in hypotheses[-1]["reason"]


def test_a_judgment_that_contradicts_the_evidence_is_withdrawn(monkeypatch):
    """after-C: the report said 18 vs 17 accepted and revenue up, the judgment said all fell."""
    row = analyses.create(question="衡简 H1 经营如何？", direction="business", is_demo=True, enqueue=False)
    run = Run(row, settings(), fabric=FakeFabric([]))
    run.p.update(
        checks=[],
        judgments=[],
        facts=[],
        report={"status": "REVIEW", "draft": "验收18台，上年17台[E2]。", "claims": []},
    )
    graded = schemas.Assessment(
        alert="important",
        judgments=[
            schemas.Judgment(
                question=q,
                conclusion=c,
                confidence="probable",
                credibility="high",
                consequence="medium",
                priority="medium",
                grounds=["E2"],
            )
            for q, c in [
                ("验收", "验收台数增加到18台"),
                ("收入", "仅部分转化：岚舟均增长；衡简验收、收入、回款均同比下降"),
            ]
        ],
    )
    drafts, sections = [], []

    def biocheck(s, operation, body):
        drafts.append(body["draft"])
        sections.append(body["sections"])
        return line_verifier(lambda line: "contradicted" if "下降" in line else "supported")(
            s, operation, body
        )

    monkeypatch.setattr(run, "ask", lambda *a, **k: graded)
    monkeypatch.setattr(run, "report_input", lambda: {"evidence": []})
    monkeypatch.setattr(services, "biocheck", biocheck)
    run.assess()
    # BioCheck cuts the clauses of a conclusion itself; the whole text is sent as a judgment section
    assert drafts == [
        "验收台数增加到18台[E2]。\n仅部分转化：岚舟均增长；衡简验收、收入、回款均同比下降[E2]。"
    ]
    assert sections == [[{"kind": "judgment", "start": 0, "end": len(drafts[0])}]]
    judgments = run.p["assessment"]["judgments"]
    assert [(j["check"], j["confidence"]) for j in judgments] == [
        ("supported", "probable"),
        ("contradicted", "insufficient"),
    ]
    assert "撤下" in judgments[1]["conclusion"]
    assert any(n["code"] == "judgment_contradicted" and "下降" in n["detail"] for n in run.p["notes"])


def test_no_budget_left_means_the_assessment_stays_unverified(monkeypatch):
    row = analyses.create(question="衡简 H1 经营如何？", direction="business", is_demo=True, enqueue=False)
    run = Run(row, settings(), fabric=FakeFabric([]))
    run.calls_used = run.input["budget"]["max_llm_calls"]
    sent = []
    monkeypatch.setattr(services, "biocheck", lambda s, operation, body: sent.append(body))
    judgment = {"conclusion": "收入增长", "grounds": ["E2"], "confidence": "probable"}
    answered = assess.check_assessment(run, {"judgments": [judgment], "hypotheses": []})
    assert sent == [] and judgment["check"] == "unverified" and answered is False
    assert run.p["notes"][-1]["code"] == "assessment_biocheck_no_budget"


def test_names_the_question_does_not_give_never_narrow_relevance(monkeypatch):
    """A plan that brings in a place the question never named keeps it as a search direction only."""
    base = fake_model([])
    reads = []

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        value, call = base(settings_, stage, system, payload, model_cls, **options)
        if stage == "plan":
            value.objects = [schemas.PlanObject(name="武汉"), schemas.PlanObject(name="XQ-17")]
            value.checks = [
                schemas.PlanCheck(text="材料是否提及华南海鲜市场暴露史", required=True, objects=[1]),
                schemas.PlanCheck(text="XQ-17 的PFS", required=True, objects=[2]),
            ]
            value.queries = [
                schemas.PlanQuery(terms=["华南海鲜"], checks=[1]),
                schemas.PlanQuery(terms=["PFS"], checks=[2]),
            ]
        if stage == "read":
            reads.append(payload)
        return value, call

    monkeypatch.setattr(llm, "chat_json", chat_json)
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    row = analyses.create(
        question="XQ-17 当前有哪些异常聚集与PFS结果？", direction="pipeline", is_demo=True, enqueue=False
    )
    result = Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    assert result["plan_basis"] == {
        "武汉": "exploratory",
        "XQ-17": "question",
        "华南海鲜": "exploratory",
        "PFS": "question",
    }
    assert reads and reads[0]["objects"] == ["XQ-17"]
    assert [c["exploratory"] for c in reads[0]["checks"]] == [["华南海鲜"], []]
    assert [c["objects"] for c in result["checks"]] == [[], ["XQ-17"]]
    assert [c["required"] for c in result["checks"]] == [False, True]  # Q1 is only about 武汉
    with db.session() as s:
        progress = s.get(db.Analysis, row.id).progress
    assert progress["plan_removed"] == [{"name": "武汉", "basis": "exploratory", "checks": ["Q1"]}]
    assert any(n["code"] == "plan_exploratory_scope" for n in result["notes"])


def test_semantic_search_is_offered_in_replays_too(monkeypatch):
    """v3 3.6: Fabric verifies every WeKnora hit at the analysis cutoff, so a replay can search
    semantically."""
    s = settings().model_copy(update={"semantic_search": True})
    for cutoff in (None, "2026-09-30T00:00:00+08:00"):
        row = analyses.create(
            question="XQ-17 的PFS？", direction="pipeline", is_demo=True, cutoff=cutoff, enqueue=False
        )
        run = Run(row, s, fabric=FakeFabric([]))
        run.p.update(
            queries=[{"terms": ["PFS"], "checks": ["Q1"]}], actions=[], pages={}, hits=[], objects=[]
        )
        offered = investigate.candidates(run)
        # a channel, not a source: the candidate names the real source it searches
        semantic = [c["source"] for c in offered if c.get("channel") == "weknora_semantic"]
        assert semantic == ["external_archive"]
        assert all(c["source"] != "weknora" for c in offered)
    off = Run(row, settings(), fabric=FakeFabric([]))
    off.p.update(queries=[{"terms": ["PFS"], "checks": ["Q1"]}], actions=[], pages={}, hits=[], objects=[])
    assert not any(c.get("channel") == "weknora_semantic" for c in investigate.candidates(off))


def test_a_plan_whose_checks_are_all_outside_the_question_answers_the_question_itself(monkeypatch):
    base = fake_model([])

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        value, call = base(settings_, stage, system, payload, model_cls, **options)
        if stage == "plan":
            value.objects = [schemas.PlanObject(name="武汉")]
            value.checks = [
                schemas.PlanCheck(text="武汉卫健委是否确认华南海鲜市场聚集", required=True, objects=[1])
            ]
            value.queries = [schemas.PlanQuery(terms=["华南海鲜"], checks=[1])]
        return value, call

    monkeypatch.setattr(llm, "chat_json", chat_json)
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    question = "当前有哪些异常聚集值得核实？"
    row = analyses.create(question=question, direction="public_health", is_demo=True, enqueue=False)
    result = Run(row, settings(), fabric=FakeFabric(RECORDS)).run()
    assert [(c["text"], c["required"]) for c in result["checks"]] == [
        ("武汉卫健委是否确认华南海鲜市场聚集", False),
        (question, True),
    ]


def test_the_planner_reads_by_term_coverage_and_puts_copies_last_while_the_baseline_keeps_its_order():
    """D-S1: literal hits came back in record-id order and unrelated English acronym hits were read first."""
    docs = {
        "a1": ("SARS officers trained", "South African Revenue Service officers trained", "2019-12-20"),
        "b2": ("兰州布病聚集", "兰州通报布病聚集病例，抗体阳性65人", "2019-12-07"),
        "c3": ("同一通报转发", "已读的那篇正文", "2019-12-08"),
        "d4": ("更正通知", "更正：病例数", "2019-12-01"),
    }
    for method, expected in (("planner", ["d4", "b2", "a1", "c3"]), ("baseline", ["d4", "a1", "b2", "c3"])):
        row = analyses.create(
            question="当前有哪些异常聚集？",
            direction="public_health",
            method=method,
            is_demo=True,
            enqueue=False,
        )
        run = Run(row, settings(), fabric=FakeFabric([]))
        hits = [
            {
                "source": "s",
                "record_id": rid,
                "title": title,
                "checks": ["Q1"],
                "critical": rid == "d4",
                "read": False,
            }
            for rid, (title, _, _) in docs.items()
        ] + [
            {
                "source": "s",
                "record_id": "r0",
                "title": "已读",
                "checks": ["Q1"],
                "critical": False,
                "read": True,
            }
        ]
        run.docs = {
            ("s", rid): {"title": t, "text": x, "published_at": {"value": d, "meaning": "source_reported"}}
            for rid, (t, x, d) in docs.items()
        }
        run.docs[("s", "r0")] = {"title": "", "text": "已读的那篇正文", "published_at": None}
        run.docs[("s", "c3")]["title"] = ""  # an exact copy of the text already read
        run.p.update(
            queries=[
                {"terms": ["布病"], "checks": ["Q1"]},
                {"terms": ["聚集"], "checks": ["Q1"]},
                {"terms": ["SARS"], "checks": ["Q1"]},
            ],
            actions=[],
            pages={},
            hits=hits,
            objects=[],
        )
        order = [c["args"]["record_id"] for c in investigate.candidates(run) if c["kind"] == "read"]
        assert order == expected, method
        # a resumed run has no texts in memory, only what progress kept: the order must not change
        import json

        again = Run(row, settings(), fabric=FakeFabric([]))
        again.p = json.loads(json.dumps(run.p))
        assert [
            c["args"]["record_id"] for c in investigate.candidates(again) if c["kind"] == "read"
        ] == expected, method


def test_same_opening_with_the_opposite_ending_is_not_a_copy():
    """A length plus 120-character key called two texts with the same opening and opposite
    conclusions copies, so the second went behind unrelated background. Only identical full texts count."""
    opening = "XQ-17 上市申请进展：" + "审评资料补充说明。" * 20
    row = analyses.create(question="XQ-17 是否获批？", direction="pipeline", is_demo=True, enqueue=False)
    run = Run(row, settings(), fabric=FakeFabric([]))
    run.docs = {
        ("s", "r0"): {"title": "", "text": opening + "现已获批", "published_at": None},
        ("s", "tail"): {"title": "", "text": opening + "现已撤回", "published_at": None},
        ("s", "same"): {"title": "", "text": opening + "现已获批", "published_at": None},
        ("s", "bg"): {"title": "", "text": "行业背景综述", "published_at": None},
    }
    run.p.update(
        queries=[{"terms": ["XQ-17"], "checks": ["Q1"]}],
        actions=[],
        pages={},
        objects=[],
        hits=[
            {
                "source": "s",
                "record_id": rid,
                "title": "",
                "checks": ["Q1"],
                "critical": False,
                "read": rid == "r0",
            }
            for rid in ("r0", "same", "bg", "tail")
        ],
    )
    expected = ["tail", "bg", "same"]
    assert [c["args"]["record_id"] for c in investigate.candidates(run) if c["kind"] == "read"] == expected
    import json

    again = Run(row, settings(), fabric=FakeFabric([]))
    again.p = json.loads(json.dumps(run.p))
    assert [c["args"]["record_id"] for c in investigate.candidates(again) if c["kind"] == "read"] == expected


def test_reposts_reach_biocheck_as_relations_with_both_ends_bound_or_marked_unread(monkeypatch):
    """C01-W2: Fabric said C01-04 reposts C01-03, but only corrections reached BioCheck, so the draft
    wrote that the materials did not say whether the repost was independent."""
    sent = []
    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(
        services, "biocheck", lambda s, op, body: sent.append((op, body)) or empty_report(s, op, body)
    )
    records = RECORDS + [
        record(
            "B51",
            "转载：XQ-17 随机试验",
            "转载：随机试验纳入240例，PFS的HR为0.62。",
            relations=[("reposts", "B31")],
        ),
        record("B61", "转载另一篇PFS", "另一篇PFS转载，原文未附。", relations=[("reposts", "B99")]),
    ]
    row = analyses.create(question="XQ-17 的PFS？", direction="pipeline", is_demo=True, enqueue=False)
    Run(row, settings(), fabric=FakeFabric(records)).run()
    with db.session() as s:
        ref = {
            e.record_id: e.ref
            for e in s.scalars(select(db.Evidence).where(db.Evidence.analysis_id == row.id))
        }
    generate = next(body for op, body in sent if op == "generate")
    assert sorted(generate["source_relations"], key=lambda r: r["target_record"]) == [
        {"kind": "repost", "source": ref["B51"], "target": ref["B31"], "target_record": "B31", "read": True},
        {"kind": "repost", "source": ref["B61"], "target": None, "target_record": "B99", "read": False},
    ]
    assert generate["source_updates"] and all(u["kind"] == "correction" for u in generate["source_updates"])
    assert all("origin_group" in e for e in generate["evidence"])


def test_question_objects_found_in_the_reviewed_graph_bring_their_source_records_to_read(monkeypatch):
    """The graph adds reading candidates with the entity's identity status kept; it decides nothing."""
    monkeypatch.setattr(llm, "chat_json", fake_model([]))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", empty_report)
    graph_record = record("G1", "所选文档当前图观测", "XQ-17 drug node", source="reviewed_graph")
    fabric = FakeFabric(RECORDS + [graph_record])
    asked = []

    def graph_resolve(doc, name, *, cutoff=None, mode=None):
        asked.append((doc["record_id"], name))
        evidence = {"source_id": "external_archive", "source_record_id": "N1", "version_id": "v" * 64}
        node = {"id": "n1", "name": "XQ-17", "identity_status": "unresolved", "evidence": [evidence]}
        return {"status": "resolved", "candidates": [node]}

    fabric.graph_resolve = graph_resolve
    base = fake_model([])

    def chat_json(settings_, stage, system, payload, model_cls, **options):
        value, call = base(settings_, stage, system, payload, model_cls, **options)
        if stage == "plan":
            value.objects = [schemas.PlanObject(name="XQ-17")]
        return value, call

    monkeypatch.setattr(llm, "chat_json", chat_json)
    row = analyses.create(question="XQ-17 的PFS？", direction="pipeline", is_demo=True, enqueue=False)
    run = Run(row, settings().model_copy(update={"graph_lookup": True}), fabric=fabric)
    run.run()
    assert asked == [("G1", "XQ-17")]
    assert run.p["graph"] == [
        {
            "object": "XQ-17",
            "graph_records": ["G1"],
            "candidates": [{"id": "n1", "name": "XQ-17", "identity": "unresolved"}],
            "refs": [
                {"source": "external_archive", "record_id": "N1", "version_id": "v" * 64, "basis": "node"}
            ],
        }
    ]
    assert any(h["record_id"] == "N1" and h["title"] == "图谱：XQ-17" for h in run.p["hits"])


def test_what_mechevidence_receives_is_recorded_with_the_binding_of_what_was_read(monkeypatch):
    """v3 6.1: the materials sent to MechEvidence are recorded per record; each binds to the same parts and
    ranges as the reading window it came from, so it reads back from the fixed-version text."""
    from bioradar.material import of, part_key, reread
    from bioradar.research.run.windows import adopt

    fabric = FakeFabric([record("M1", "机制研究", "甲" * 30000 + "X 抑制 Y。", kind="primary_research")])
    doc = fabric.docs[0]
    row = analyses.create(question="X 是否抑制 Y？", direction="science", is_demo=True, enqueue=False)
    run = Run(row, settings().model_copy(update={"mechevidence_url": "http://mech.invalid"}), fabric=fabric)
    run.p.update(facts=[{"ref": "E1"}], mechanism={"statement": "X 抑制 Y"})
    material, window = adopt(of(doc), ["抑制"], 2000)
    with db.session() as s:
        read_id = submit.sent(run, s, "read", "radar", "E1", doc, material)
        s.add(
            db.Evidence(
                analysis_id=run.id,
                ref="E1",
                source=doc["source"],
                record_id=doc["record_id"],
                version_id=doc["version_id"],
                title=doc["title"],
                material_kind="primary_research",
                verified=True,
                text=material.text,
                meta={"material_read_id": read_id, "window": window},
            )
        )
    bodies = []
    monkeypatch.setattr(services, "mechevidence", lambda s, body: bodies.append(body) or {"llm_calls": []})
    run.mechanism()
    assert run.p["mechanism_result"]["status"] == "completed"
    with db.session() as s:
        mine = select(db.MaterialRead).where(db.MaterialRead.analysis_id == run.id)
        rows = {r.stage: r for r in s.scalars(mine)}
    assert set(rows) == {"read", "mechanism"} and rows["mechanism"].service == "mechevidence"
    assert rows["mechanism"].binding == rows["read"].binding == material.binding()
    resolve = lambda parts: {part_key(q): doc["text"] for q in parts}  # noqa: E731
    assert reread(rows["mechanism"].binding, resolve) == bodies[0]["materials"][0]["text"] == material.text


def test_every_outbound_attempt_keeps_its_own_record_and_status():
    """A read sent and then repeated after a restart is two records, not one rewritten; each says
    whether it went out (sent), was not sent (no model budget) or failed, and a sent one stays sent."""
    import pytest

    from bioradar.material import of

    row = analyses.create(question="重试？", direction="pipeline", is_demo=True, enqueue=False)
    run = Run(row, settings(), fabric=None)
    doc = FakeFabric(RECORDS).docs[0]
    with db.session() as s:
        first = submit.sent(run, s, "read", "radar", "E1", doc, of(doc))
    with submit.outbound(run, [first]):
        pass  # the service answered
    with db.session() as s:
        second = submit.sent(
            run, s, "read", "radar", "E1", doc, of(doc)
        )  # the worker died; the same read again
        third = submit.sent(run, s, "report", "biocheck", "E1", doc, of(doc))
    with submit.outbound(run, [second], model=True):
        pass  # no model call was made
    with pytest.raises(RuntimeError), submit.outbound(run, [third]):
        raise RuntimeError("biocheck down")
    submit.mark(run, [first], "failed")  # a later failure does not undo what was already sent
    with db.session() as s:
        rows = list(s.scalars(select(db.MaterialRead).where(db.MaterialRead.analysis_id == run.id)))
    assert sorted((r.stage, r.attempt, r.status) for r in rows) == [
        ("read", 1, "sent"),
        ("read", 2, "not_sent"),
        ("report", 1, "failed"),
    ]
    assert rows[0].binding == rows[1].binding


def test_text_the_cleaning_layer_did_not_produce_says_so_to_every_receiver(monkeypatch):
    """When Fabric's cleaned text is not there the version's own text is read, but the reader and
    BioCheck receive it with the notice in the text (a fixed template, bound as such), and the analysis notes
    it; the text level is not full text."""
    from bioradar.adapters.fabric import UNCLEANED
    from bioradar.material import Material

    sent, biocheck = [], []
    monkeypatch.setattr(llm, "chat_json", fake_model(sent))
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(
        services,
        "biocheck",
        lambda s, op, body: (
            biocheck.append(body)
            or (
                {"draft": "同一试验更正后PFS的HR为0.69[E1]。", "llm_calls": []}
                if op == "generate"
                else {"status": "PASS", "claims": [], "coverage": {"verdict": "complete"}, "llm_calls": []}
            )
        ),
    )
    fabric = FakeFabric(RECORDS)
    for d in fabric.docs:  # what Fabric.attach_clean_text does when the cleaning layer has nothing
        d["material"] = Material.literal(UNCLEANED.format(state="failed")) + d["material"]
        d["text"], d["clean_text_state"] = d["material"].text, "failed"
        d["text_level"] = "uncleaned_projection"
    row = analyses.create(
        question="XQ-17 的PFS结果现在应怎样判断？", direction="pipeline", is_demo=True, enqueue=False
    )
    result = Run(row, settings(), fabric=fabric).run()
    notice = UNCLEANED.format(state="failed").strip()
    reads = [p["material"]["text"] for _, p, _ in sent if isinstance(p, dict) and "material" in p]
    assert reads and all(t.startswith(notice) for t in reads)
    assert biocheck and all(notice in e["text"] for e in biocheck[0]["evidence"])
    assert "clean_text_unprocessed" in [n["code"] for n in result["notes"]]
    with db.session() as s:
        evidence = list(s.scalars(select(db.Evidence).where(db.Evidence.analysis_id == row.id)))
    assert evidence and all(e.meta["text_level"] == "uncleaned_projection" for e in evidence)


def test_the_notice_heads_every_window_of_a_long_uncleaned_record():
    """A long uncleaned text cut to the paragraphs that matter, then to the quoted passage for the
    report, loses its first line; the notice is put back on every window sent, once."""
    from bioradar.adapters.fabric import UNCLEANED, noticed
    from bioradar.material import Material
    from bioradar.research.run.windows import adopt, report_text

    notice = UNCLEANED.format(state="failed")
    lines = ["无关段落" * 50] * 100 + ["关键结果 HR为0.69"]
    body = Material.part("\n".join(lines), {"kind": "projection_text"})
    record = Material.literal(notice) + body
    read, window = adopt(record, ["关键"], 2000)
    assert window == "selected" and not read.text.startswith(notice)  # the cut alone would drop it
    sent = noticed(read, "failed")
    assert sent.text.startswith(notice) and "HR为0.69" in sent.text
    report = noticed(report_text(sent, ["HR为0.69"], 300), "failed")
    assert report.text.startswith(notice) and "HR为0.69" in report.text
    assert noticed(report, "failed").text.count(notice) == 1 and noticed(read, "processed") is read


def test_each_biocheck_request_has_its_own_outbound_record(monkeypatch):
    """Generate succeeded, then verify failed; the two requests carrying the same report windows
    are two records (report:generate sent, report:verify failed), never one sent record hiding the failure."""
    from types import SimpleNamespace

    import pytest

    from bioradar.material import of

    row = analyses.create(question="审计？", direction="pipeline", is_demo=True, enqueue=False)
    run = Run(row, settings(), fabric=None)
    doc = FakeFabric(RECORDS).docs[0]
    evidence = SimpleNamespace(
        ref="E1", source=doc["source"], record_id=doc["record_id"], version_id=doc["version_id"], meta={}
    )
    run.report_windows = [(evidence, of(doc))]
    body = {"evidence": [{"id": "E1", "text": doc["text"]}]}

    def biocheck(s, operation, body):
        if operation == "verify":
            raise RuntimeError("biocheck down")
        return {"draft": "稿", "llm_calls": []}

    monkeypatch.setattr(services, "biocheck", biocheck)
    submit.service(run, "generate", dict(body))
    with pytest.raises(RuntimeError):
        submit.service(run, "verify", dict(body))
    with db.session() as s:
        rows = list(s.scalars(select(db.MaterialRead).where(db.MaterialRead.analysis_id == run.id)))
    assert sorted((r.stage, r.attempt, r.status) for r in rows) == [
        ("report:generate", 1, "sent"),
        ("report:verify", 1, "failed"),
    ]
    assert rows[0].binding == rows[1].binding  # the same content, bound the same way


def test_the_window_of_a_long_record_goes_from_the_reader_to_biocheck(monkeypatch):
    """A long record is read, then reported, as excerpts. The reader and every BioCheck request are told
    which characters of the record the text holds and how long the record is, so that what lies outside
    (here the risk sentence at the end) is not taken for missing from the record; a row stored without the
    layout says unknown, never the whole record."""
    from bioradar.material import Material
    from bioradar.research.run import report
    from bioradar.research.run.windows import record_layout, window_range

    risk = "The risk in the Democratic Republic of the Congo was assessed as very high."
    text = (
        "XQ-17 PFS的HR为0.69。\n"
        + "\n".join(f"第{i}段背景说明。" + "无关内容" * 60 for i in range(30))
        + "\n"
        + risk
        + "，"
        + "尾部说明" * 100
    )
    reads, biocheck, sent = [], [], []
    base = fake_model(sent)

    def model(settings_, stage, system, payload, model_cls, **options):
        value, call = base(settings_, stage, system, payload, model_cls, **options)
        if stage == "read":
            reads.append(payload["material"])
            value = schemas.Reading(
                relevant=True,
                facts=[
                    schemas.ReadFact(text="PFS风险比", quote="HR为0.69", checks=["Q1"], stance="supports")
                ],
                answered=["Q1"],
            )
        return value, call

    def fake_biocheck(s, operation, body):
        biocheck.append(body)
        if operation == "generate":
            return {"draft": "PFS的HR为0.69[E1]。", "llm_calls": []}
        return {"status": "PASS", "claims": [], "coverage": {"verdict": "complete"}, "llm_calls": []}

    monkeypatch.setattr(llm, "chat_json", model)
    monkeypatch.setattr(services, "decide", lambda s, body: first_policy(body))
    monkeypatch.setattr(services, "biocheck", fake_biocheck)
    row = analyses.create(question="XQ-17 的PFS结果？", direction="pipeline", is_demo=True, enqueue=False)
    # a record of 8,000 characters is long enough when the reader gets 5,000
    short = settings().model_copy(update={"read_characters": 5000})
    run = Run(row, short, fabric=FakeFabric([record("L1", "XQ-17 长文", text)], clean=True))
    run.run()

    def ranges_of(window, shown):
        """The window says: an excerpt of this record, whose ranges are where the text sent really came
        from, and the risk sentence at the record's end lies in none of them and is not in the text."""
        assert window["coverage"] == "excerpt" and window["record_chars"] == len(text)
        assert risk not in shown and not any(a <= text.index(risk) < b for a, b in window["ranges"])
        assert all(text[a : a + 10] in shown and text[b - 10 : b] in shown for a, b in window["ranges"])
        return window["ranges"]

    assert len(reads) == 1 and len(text) > short.read_characters
    read_ranges = ranges_of(reads[0]["window"], reads[0]["text"])
    assert len(biocheck) >= 2  # generate and verify
    for body in biocheck:
        (evidence,) = body["evidence"]
        assert evidence["id"] == "E1" and len(evidence["text"]) < len(reads[0]["text"])
        assert (
            ranges_of(evidence["window"], evidence["text"]) != read_ranges
        )  # cut again from the read window
    with db.session() as s:
        stored = s.scalars(select(db.Evidence).where(db.Evidence.analysis_id == row.id)).one()
        meta = dict(stored.meta)
        meta.pop("record_layout")  # as a row stored before the layout was kept
        stored.meta = meta
    assert [e["window"] for e in report.report_input(run)["evidence"]] == [{"coverage": "unknown"}]
    whole = Material.part("甲" * 50, {"kind": "clean_text"})
    layout = record_layout(whole)
    assert window_range(whole, layout) == {"coverage": "full"}
    assert window_range(whole.slice(0, 5), layout)["coverage"] == "excerpt"
    assert window_range(whole, None) == {"coverage": "unknown"}
