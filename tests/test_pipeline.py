"""One analysis end to end with fake Fabric, model and services."""

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
                        grounds=["E2", "E9"],
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
            return {"draft": "同一试验更正后PFS的HR为0.69[E2]。", "llm_calls": call}
        return {"status": "PASS", "claims": [], "coverage": {"verdict": "complete"}, "llm_calls": call}

    monkeypatch.setattr(services, "biocheck", fake_biocheck)
    row = analyses.create(
        question="XQ-17 的PFS结果现在应怎样判断？", direction="pipeline", is_demo=True, enqueue=False
    )
    result = Run(row, settings(), fabric=FakeFabric(RECORDS)).run()

    with db.session() as s:
        evidence = list(s.scalars(select(db.Evidence).order_by(db.Evidence.ref)))
    assert [(e.ref, e.record_id) for e in evidence] == [("E1", "B31"), ("E2", "B41")]
    assert result["report_status"] == "PASS"
    # The invented quote is dropped, the real ones stay.
    assert [n["code"] for n in result["notes"]].count("quote_not_found") == 2
    assert result["checks"][0]["status"] == "answered" and result["checks"][0]["evidence"] == ["E1", "E2"]
    # The correction reaches BioCheck as a source update, old -> new.
    generate = biocheck[0][1]
    assert generate["source_updates"] == [{"target": "E1", "replacement": "E2", "kind": "correction"}]
    assert {e["source_kind"] for e in generate["evidence"]} == {"official"}
    # The number is computed by the program, not by the model.
    assert result["computations"][0]["value"] == 31.0
    assert generate["analysis_results"][0]["id"] == "A1"
    # Judgment grounds keep only refs the verified report actually cites.
    assert result["assessment"]["judgments"][0]["grounds"] == ["E2"]
    # Structured stages run with thinking off (the default of chat_json).
    assert all(not options.get("thinking") for _, _, options in sent)
    assert (
        result["usage"]["llm_calls"] == 1 + 2 + 1 + 1 + 2
    )  # plan, two reads, calculate, assess, BioCheck x2


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
