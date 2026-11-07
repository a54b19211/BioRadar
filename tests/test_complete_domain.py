"""Complete domain: a number is compared automatically only in a group whose
metric family is determined and whose required dimensions are all proven - checked, or carried by a matched
field with its full phrase. Gate cases D1-D12 on stored check records; grouping on a stand-in for checker v11
(stated dimensions counted as checked) - the real v11 chain is still to be verified."""

from datetime import timedelta
from types import SimpleNamespace

from conftest import FakeFabric, biocheck_fake, record
from sqlalchemy import select

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.research import domain, measure_checks, monitor, scheduling, schemas, topics


def check(**fields):
    """A stored check: {name: (value, result)} -> request and response of one item; a value with a span."""
    asked, answered = {"scope_mode": fields.pop("scope_mode", "explicit")}, {}
    for i, (name, (value, result)) in enumerate(fields.items()):
        asked[name] = {
            "state": "stated",
            "value": value,
            "span": {"passage": "p1", "start": i * 10, "end": i * 10 + 5},
        }
        answered[name] = {"result": result}
    return SimpleNamespace(
        id=7, request={"items": [{"fields": asked}]}, response={"items": [{"fields": answered}]}
    )


CASES = dict(metric=("新增确诊病例", "match"), scope=("湖北省", "match"), value=("23", "match"))


def dims(**kinds):
    return [{"kind": k, "text": v} for k, v in kinds.items()]


def gate(family="case_count", stated=None, kind="flow", **fields):
    m = {"family": family, "dims": stated or []}
    return domain.series_gate(
        m, {"measure_kind": kind, "metric": fields.get("metric", ("",))[0]}, check(**(fields or CASES))
    )


def test_a_case_count_with_stated_dimensions_is_not_compared_until_they_are_checked():
    g = gate(stated=dims(disease="炭疽", population="成人", date_basis="按报告日"), **CASES)
    assert g["reasons"] == ["dim_unchecked:disease", "dim_unchecked:population", "dim_unchecked:date_basis"]
    carried = {d["kind"]: d for d in g["dims"] if d["state"] == "carried"}
    assert carried["case_definition"]["proof"] == {
        "field": "metric",
        "phrase": "新增确诊病例",
        "span": {"passage": "p1", "start": 0, "end": 5},
        "check_id": 7,
        "result": "match",
    }
    assert carried["coverage"]["norm"] == "湖北省"


def test_d4_d5_unknown_dimensions_on_both_sides_are_not_compared_either():
    g = gate(stated=dims(disease="炭疽"), **CASES)
    assert "dim_unknown:population" in g["reasons"] and "dim_unknown:date_basis" in g["reasons"]


def test_d8_a_family_not_named_or_not_compatible_with_the_matched_metric_is_not_determined():
    assert gate(family="", **CASES)["reasons"] == ["family_not_determined"]
    assert gate(metric=("批准注册医疗器械产品", "match"), scope=("全国", "match"))["reasons"] == [
        "family_not_determined"
    ]  # a registration count is not a case count, whatever the label says
    assert gate(metric=("新增确诊病例", "unsupported"), scope=("湖北省", "match"))["reasons"] == [
        "family_not_determined"
    ]  # an unmatched metric admits no family


def test_d9_efficacy_is_never_compared_automatically():
    assert gate(family="efficacy", **CASES)["reasons"] == ["efficacy_not_compared"]


def test_d10_a_rate_needs_its_denominator_definition():
    g = gate(kind="rate", stated=dims(disease="炭疽", population="成人", date_basis="按报告日"), **CASES)
    assert "dim_unknown:denominator_definition" in g["reasons"]
    # the denominator field is the number (contract v2): a matched 100 proves no definition
    g = gate(kind="rate", stated=dims(disease="炭疽"), denominator=("100", "match"), **CASES)
    assert "dim_unknown:denominator_definition" in g["reasons"]
    g = gate(kind="rate", stated=dims(disease="炭疽", denominator_definition="检测人数"), **CASES)
    assert "dim_unchecked:denominator_definition" in g["reasons"]  # stated: checked by v11, not carried


def test_an_indication_the_matched_metric_names_is_required_and_carried_with_its_phrase():
    g = gate("milestone_count", stated=dims(date_basis="按批准日"), metric=("甲适应症获批项目", "match"))
    ind = next(d for d in g["dims"] if d["kind"] == "indication")
    assert ind["state"] == "carried" and ind["proof"]["phrase"] == "甲适应症获批项目"
    assert "dim_unknown:product_or_class" in g["reasons"]  # an object proves no product class


def test_the_group_key_is_a_lossless_identity_never_truncated():
    long = "符合本研究纳入条件的成年受试者" * 40
    a = gate(stated=dims(disease="炭疽", population=long + "甲", date_basis="按报告日"), **CASES)
    b = gate(stated=dims(disease="炭疽", population=long + "乙", date_basis="按报告日"), **CASES)
    assert a["dims_key"] != b["dims_key"] and a["dims_key"].startswith("sha256:") and len(a["dims_key"]) == 71
    c = gate(stated=dims(disease="炭疽;population=x", date_basis="按报告日"), **CASES)
    d = gate(stated=dims(disease="炭疽", population="x", date_basis="按报告日"), **CASES)
    assert c["dims_key"] != d["dims_key"]  # no separator inside a value makes two groups one


def test_d11_a_dimension_carried_by_an_unmatched_field_is_not_proven():
    g = gate(stated=dims(disease="炭疽"), metric=("新增确诊病例", "match"), scope=("湖北省", "unsupported"))
    assert "dim_unknown:coverage" in g["reasons"]


def test_carried_keeps_the_whole_qualifying_phrase():
    plan = gate(
        "capacity",
        stated=dims(entity="甲公司", date_basis="按季度"),
        metric=("计划新建灌装线", "match"),
        scope=("全国", "match"),
        object=("甲公司", "match"),
    )
    run = gate(
        "capacity",
        stated=dims(entity="甲公司", date_basis="按季度"),
        metric=("投运灌装线", "match"),
        scope=("全国", "match"),
        object=("甲公司", "match"),
    )
    stage = {g["dims_key"] for g in (plan, run)}
    assert len(stage) == 2  # a plan and an operating line are never one group
    assert next(d for d in plan["dims"] if d["kind"] == "stage")["proof"]["phrase"] == "计划新建灌装线"


def test_a_milestone_naming_an_indication_must_prove_it():
    g = gate(
        "milestone_count",
        stated=dims(indication="非小细胞肺癌", date_basis="按批准日"),
        metric=("批准上市新药", "match"),
        object=("某药", "match"),
    )
    assert "dim_unchecked:indication" in g["reasons"]
    g = gate(
        "milestone_count",
        stated=dims(date_basis="按批准日"),
        metric=("批准上市新药", "match"),
        object=("某药", "match"),
    )
    assert not any("indication" in r for r in g["reasons"])  # not named: not required


def test_d12_a_whole_population_counts_only_as_written():
    written = gate(stated=dims(disease="炭疽", population="全人群", date_basis="按报告日"), **CASES)
    assert next(d for d in written["dims"] if d["kind"] == "population")["norm"] == "全人群"
    implied = gate(
        stated=dims(disease="炭疽", date_basis="按报告日"),
        metric=("合计病例", "match"),
        scope=("湖北省", "match"),
    )
    assert "dim_unknown:population" in implied["reasons"]  # "合计" or total is never taken as a population


# ---- adopt: only complete, proven groups compute -----------------------------------------------------------

CALL = {"stage": "triage", "model": "fake", "finish_reason": "stop", "usage": {}, "seconds": 0, "error": None}


def feed(monkeypatch, specs):
    """specs: (record id, month, value, {dim: text}); one tick per record, all placed in topic 炭疽."""
    post = biocheck_fake()
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: post(body))
    by_text, records = {}, []
    for day_, (rid, month, value, stated) in enumerate(specs, 1):
        sentence = f"2026年{month}月医院A在湖北省新增确诊病例{value}例。"
        text = f"# 炭疽\n\n{sentence}"
        by_text[text] = schemas.Measure(
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
            family="case_count",
            dims=[schemas.Dim(kind=k, text=v) for k, v in stated.items()],
        )
        records.append(record(rid, "炭疽", text, available=f"2026-06-{day_:02d}T08:00:00+08:00"))

    def triage(settings_, stage, system, payload, model_cls, **options):
        if stage == "extract":
            return schemas.Extraction(
                items=[schemas.ExtractItem(index=w["index"]) for w in payload["windows"]]
            ), dict(CALL, stage=stage)
        index = {t["title"]: t["index"] for t in payload["topics"]}
        return schemas.Triage(
            items=[
                schemas.TriageItem(
                    index=r["index"],
                    topic=index.get("炭疽"),
                    new_topic="" if "炭疽" in index else "炭疽",
                    direction="public_health",
                    alert="normal",
                    measures=[by_text[r["text"]]],
                )
                for r in payload["records"]
            ]
        ), CALL

    monkeypatch.setattr(llm, "chat_json", triage)
    for n in range(1, len(records) + 1):
        monitor.tick(is_demo=True, fabric=FakeFabric(records[:n], clean=True))
    with db.session() as s:
        points = {p.record_id: p for p in s.scalars(select(db.Measurement))}
        events = [
            (e.detail["record"], sorted(c["kind"] for c in e.detail["changes"]))
            for e in s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "series"))
        ]
        states = {r: measure_checks.comparability(s, p) for r, p in points.items()}
    return points, events, states


FULL = {"disease": "炭疽", "population": "成人", "date_basis": "按报告日"}


def test_with_the_real_gate_nothing_is_computed_until_dimensions_are_checked(monkeypatch):
    points, events, states = feed(monkeypatch, [("A1", 1, 10, FULL), ("A2", 1, 20, FULL)])
    assert len(points) == 2 and events == []  # same month, 10 and 20: no conflict computed
    assert states["A2"]["state"] == "not_comparable" and "dim_unchecked:disease" in states["A2"]["reasons"]


def v11_stand_in(monkeypatch):
    """Stated dimensions counted as checked (what checker v11 is to prove per dimension) - grouping only."""
    real = domain.series_gate

    def gate(measure, final, check):
        g = real(measure, final, check)
        for d in g["dims"]:
            if d["state"] == "stated_unchecked":
                d["state"] = "checked"
        g["reasons"] = [r for r in g["reasons"] if not r.startswith("dim_unchecked:")]
        return g

    monkeypatch.setattr(domain, "series_gate", gate)


def test_d1_d2_same_disease_other_cohort_or_date_basis_is_another_group(monkeypatch):
    v11_stand_in(monkeypatch)
    child = dict(FULL, population="儿童")
    onset = dict(FULL, date_basis="按发病日")
    points, events, states = feed(
        monkeypatch, [("A1", 1, 10, FULL), ("K1", 1, 20, child), ("O1", 1, 30, onset), ("A2", 1, 40, FULL)]
    )
    kinds = {r: k for r, k in events}
    assert "conflict" not in kinds.get("K1", []) and "conflict" not in kinds.get("O1", [])
    assert "conflict" in kinds["A2"]  # the same complete group, the same month: a conflict
    assert states["A2"]["state"] == "comparable"


def test_d4_a_point_with_an_unknown_dimension_is_never_grouped_with_one_that_states_it(monkeypatch):
    v11_stand_in(monkeypatch)
    points, events, states = feed(
        monkeypatch, [("A1", 1, 10, FULL), ("U1", 1, 20, {"disease": "炭疽", "date_basis": "按报告日"})]
    )
    assert events == [] and "dim_unknown:population" in states["U1"]["reasons"]


def test_one_record_stating_one_identity_with_two_values_keeps_both_as_a_contradiction(monkeypatch):
    """The same record version, group and period, 10 and 30 in two sentences - both stored with
    the
    gap record_internal_conflict, never compared, the topic told; the same value twice is one point."""
    v11_stand_in(monkeypatch)
    sent = [
        "首次统计，2026年1月医院A在湖北省新增确诊病例{v}例。",
        "另据本通报，2026年1月医院A在湖北省新增确诊病例{v}例。",
    ]
    post = biocheck_fake()
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: post(body))
    for values, expect in (((10, 30), 2), ((10, 10), 1)):
        text = "# 炭疽\n\n" + "\n\n".join(q.format(v=v) for q, v in zip(sent, values))
        measures = [
            schemas.Measure(
                object_text="医院A",
                metric_text="新增确诊病例",
                value_text=str(v),
                unit_text="例",
                period_text="2026年1月",
                period="2026-01",
                period_type="interval",
                kind="flow",
                scope_text="在湖北省",
                qualifier="exact",
                modality="actual",
                status="reported",
                quote=q.format(v=v)[:-1],
                family="case_count",
                dims=[schemas.Dim(kind=k, text=x) for k, x in FULL.items()],
            )
            for q, v in zip(sent, values)
        ]
        rid = f"R{values[1]}"

        def triage(settings_, stage, system, payload, model_cls, **options):
            if stage == "extract":
                return schemas.Extraction(
                    items=[schemas.ExtractItem(index=w["index"]) for w in payload["windows"]]
                ), dict(CALL, stage=stage)
            return schemas.Triage(
                items=[
                    schemas.TriageItem(
                        index=r["index"],
                        new_topic="炭疽",
                        direction="public_health",
                        alert="normal",
                        measures=measures,
                    )
                    for r in payload["records"]
                ]
            ), CALL

        monkeypatch.setattr(llm, "chat_json", triage)
        monitor.tick(is_demo=True, fabric=FakeFabric([record(rid, "炭疽", text)], clean=True))
        with db.session() as s:
            points = list(s.scalars(select(db.Measurement).where(db.Measurement.record_id == rid)))
            assert len(points) == expect, [(p.value_text, p.dims_reasons) for p in points]
            if expect == 2:
                assert all("record_internal_conflict" in p.dims_reasons for p in points)
                assert {measure_checks.comparability(s, p)["state"] for p in points} == {"not_comparable"}
                [e] = s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "record_conflict"))
                assert sorted(x["value"] for x in e.detail["points"]) == ["10", "30"]
            else:
                assert "record_internal_conflict" not in points[0].dims_reasons


def test_the_analysis_a_record_contradiction_triggers_receives_both_points_and_the_pinned_version(
    monkeypatch,
):
    """The queued analysis gets both points (their check and comparability state) and the record
    at
    the version that contradicts itself as a pinned seed; the question names both values."""
    test_one_record_stating_one_identity_with_two_values_keeps_both_as_a_contradiction(monkeypatch)
    monkeypatch.setattr(monitor.analyses, "dispatch", lambda analysis_id: None)
    monitor.start_analyses(monitor.settings(), is_demo=True, cutoff=None)
    with db.session() as s:
        points = list(s.scalars(select(db.Measurement).where(db.Measurement.record_id == "R30")))
        [e] = s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "record_conflict"))
        analysis = s.get(db.Analysis, s.get(db.Topic, e.topic_id).last_analysis_id)
        got = analysis.input
    assert {p.id for p in points} <= {x["measurement_id"] for x in got["series_points"]}
    assert all(
        x["comparability"]["state"] == "not_comparable"
        for x in got["series_points"]
        if x["measurement_id"] in {p.id for p in points}
    )
    pinned = [x for x in got["seed_records"] if x.get("version_id") == points[0].version_id]
    assert [x["record_id"] for x in pinned] == ["R30"]
    assert "原文内部矛盾" in got["question"] and "10例" in got["question"] and "30例" in got["question"]


def conflict_topic(monkeypatch, extra):
    """The R30 contradiction plus `extra` more record_conflict events naming the same two points, the later
    ones sharing one time."""
    monkeypatch.setattr(monitor.analyses, "dispatch", lambda analysis_id: None)
    test_one_record_stating_one_identity_with_two_values_keeps_both_as_a_contradiction(monkeypatch)
    with db.session() as s:
        [first] = s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "record_conflict"))
        topic = s.get(db.Topic, first.topic_id)
        points = first.detail["points"]
        for n in range(extra):
            at = monitor.parse_time(first.at) + timedelta(seconds=min(n, 5))
            topics.event(
                s,
                topic,
                at,
                "record_conflict",
                record=f"X{n}",
                version=f"v{n}",
                period="2026-01",
                points=points,
            )
            topics.owe(topic, "record_conflict", at)
        return topic.id


def conflict_rounds(tid, rounds):
    """Run start_analyses up to `rounds` times (each a fresh call, as after a restart); the contradiction
    records each analysis of the topic was handed, and whether the topic was still owed between rounds."""
    owed = []
    for _ in range(rounds):
        monitor.start_analyses(monitor.settings(), is_demo=True, cutoff=None)
        with db.session() as s:
            pending = s.get(db.Topic, tid).pending_reason
        if not pending:
            break
        owed.append(pending)
    with db.session() as s:
        batches = [
            [x["record_id"] for x in a.input["seed_records"] if x.get("title") == "原文内部矛盾"]
            for a in s.scalars(select(db.Analysis).order_by(db.Analysis.id))
            if a.input.get("topic_id", tid) == tid
        ]
    return [b for b in batches if b], owed


def test_a_bounded_conflict_hand_off_settles_only_what_it_handed_and_carries_on(monkeypatch):
    """12 contradictions (six at one time) - batches of at most 10, every record handed exactly
    once across fresh start_analyses calls, the reason owed until the last one is handed."""
    tid = conflict_topic(monkeypatch, 11)
    batches, owed = conflict_rounds(tid, 4)
    handed = [r for b in batches for r in b]
    assert all(len(b) <= 10 for b in batches) and len(batches) == 2, batches
    assert sorted(handed) == sorted(set(handed)) and set(handed) == {"R30"} | {f"X{n}" for n in range(11)}
    assert owed and all("record_conflict" in p for p in owed)
    with db.session() as s:
        topic = s.get(db.Topic, tid)
        assert topic.pending_reason is None
        handed_ids = scheduling.handed_events(s, tid)
        conflicts = {
            e.id for e in s.scalars(select(db.TopicEvent).where(db.TopicEvent.kind == "record_conflict"))
        }
        assert conflicts <= handed_ids


def test_a_conflict_hand_off_of_many_batches_keeps_events_of_one_time_apart(monkeypatch):
    """22 contradictions, seventeen at one time - three batches, none skipped, none repeated."""
    tid = conflict_topic(monkeypatch, 21)
    batches, owed = conflict_rounds(tid, 5)
    handed = [r for b in batches for r in b]
    assert sorted(len(b) for b in batches) in ([2, 10, 10], [1, 10, 10]), batches
    assert sorted(handed) == sorted(set(handed)) and {f"X{n}" for n in range(21)} <= set(handed)
    assert len(owed) == len(batches) - 1


def test_a_correction_batch_hands_repeats_with_their_version_and_leaves_the_rest_owed(monkeypatch):
    """Twelve correcting record versions (one repeated) - a batch of ten with the repeat, the
    earliest time of the two left over, and once handed the next batch is exactly those two."""
    tid = conflict_topic(monkeypatch, 0)
    with db.session() as s:
        topic = s.get(db.Topic, tid)
        t0 = monitor.parse_time(topic.pending_since or db.now())
        for n in list(range(12)) + [3]:
            topics.event(
                s,
                topic,
                t0 + timedelta(seconds=min(n, 9)),
                "correction",
                record=f"C{n}",
                by_source="s",
                by_version_id=f"b{n}",
                reason="source_corrected",
            )
        batch, given, left = scheduling.correction_batch(s, tid, t0)
        assert [e.detail["record"] for e in batch] == [f"C{n}" for n in range(10)]
        assert len(given) == 11 and left == t0 + timedelta(seconds=9)
        topics.event(
            s, topic, db.now(), "analysis", analysis_id=None, reason="source_corrected", handed_events=given
        )
        s.flush()
        batch, given, left = scheduling.correction_batch(s, tid, t0)
        assert [e.detail["record"] for e in batch] == ["C10", "C11"] and left is None
