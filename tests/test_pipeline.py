"""One analysis end to end with fake Fabric, model and services."""

import re

from conftest import FakeFabric, record
from sqlalchemy import select

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.config import settings
from bioradar.research import analyses, schemas
from bioradar.research.pipeline import Run

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
    """Stand-in for query-planner: search first, then read in order, stop when nothing is left."""
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
    # the planner reads the newer of two equally matching records first (T-21), so the correction is E1
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
    assert all(call[3] == () for call in fabric.calls if call[0] == "search")
    with db.session() as s:
        assert [e.record_id for e in s.scalars(select(db.Evidence))] == ["B31"]  # B41 is outside the set


def test_retries_of_empty_searches_are_capped(monkeypatch):
    from bioradar.research.pipeline import MAX_RETRIES

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


def test_excerpt_finds_a_quote_whose_spacing_differs_from_the_source():
    from bioradar.research.pipeline import excerpt

    text = "甲" * 5000 + "PFS的HR\n为 0.69" + "乙" * 5000
    assert "0.69" in excerpt(text, ["PFS的HR为0.69"], limit=2000)


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
    from bioradar.research.pipeline import PLANNER_CHECK

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
    drafts = []

    def biocheck(s, operation, body):
        drafts.append(body["draft"])
        return line_verifier(lambda line: "contradicted" if "下降" in line else "supported")(
            s, operation, body
        )

    monkeypatch.setattr(run, "ask", lambda *a, **k: graded)
    monkeypatch.setattr(run, "report_input", lambda: {"evidence": []})
    monkeypatch.setattr(services, "biocheck", biocheck)
    run.assess()
    assert drafts == [
        "验收台数增加到18台[E2]。\n仅部分转化：岚舟均增长[E2]。衡简验收、收入、回款均同比下降[E2]。"
    ]
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
    run.check_assessment({"judgments": [judgment], "hypotheses": []})
    assert sent == [] and judgment["check"] == "unverified"
    assert run.p["notes"][-1]["code"] == "assessment_biocheck_no_budget"


def test_names_the_question_does_not_give_never_narrow_relevance(monkeypatch):
    """T-22: a plan that brings in a place the question never named keeps it as a search direction only."""
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


def test_a_replay_does_not_search_the_bridge_index(monkeypatch):
    """The bridge searches today's index: in a replay it would pick records by text written later."""
    s = settings().model_copy(update={"weknora_url": "http://bridge.invalid"})
    for cutoff, expected in ((None, True), ("2026-09-30T00:00:00+08:00", False)):
        row = analyses.create(
            question="XQ-17 的PFS？", direction="pipeline", is_demo=True, cutoff=cutoff, enqueue=False
        )
        run = Run(row, s, fabric=FakeFabric([]))
        run.p.update(
            queries=[{"terms": ["PFS"], "checks": ["Q1"]}], actions=[], pages={}, hits=[], objects=[]
        )
        assert any(c["source"] == "weknora" for c in run.candidates()) is expected


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
        run.docs = {("s", rid): {"title": t, "text": x, "published_at": d} for rid, (t, x, d) in docs.items()}
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
        order = [c["args"]["record_id"] for c in run.candidates() if c["kind"] == "read"]
        assert order == expected, method


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
