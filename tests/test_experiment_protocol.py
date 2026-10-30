"""Offline protocol controls; these fixtures are not scientific experiment results."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "experiment_protocol", Path(__file__).parents[1] / "evaluation/experiment_protocol.py"
)
protocol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(protocol)


def _bytes(value):
    return json.dumps(value, sort_keys=True).encode()


def _experiment(scenario="A", with_controls=False):
    endpoints = ["endpoint-one"] if scenario != "D" else ["endpoint-one", "endpoint-two", "endpoint-three"]
    ids = ["positive"] + (["seasonal", "false-1", "false-2", "false-3"] if with_controls else [])
    packet = {
        "experiment_id": "OFFLINE-PROTOCOL-CONTROL",
        "scenario": scenario,
        "cases": [
            {
                "case_id": case_id,
                "messages": [
                    {
                        "message_id": "public-1",
                        "available_at": "2020-01-01T00:00:00Z",
                        "time_precision": "second",
                        "text": "Offline public input",
                    },
                ],
            }
            for case_id in ids
        ],
    }
    rules = {
        "experiment_id": packet["experiment_id"],
        "scenario": scenario,
        "endpoint_ids": endpoints,
        "max_inappropriate_control_alerts": 0,
        "false_alarm_definition": (
            "Independent review of the frozen-time action; verification is not an alarm."
        ),
    }
    settings = {"model": "offline-no-model", "budget": {"tokens": 100}}
    answers = {
        "experiment_id": packet["experiment_id"],
        "cases": [
            {
                "case_id": case_id,
                "control_role": "same_season"
                if case_id == "seasonal"
                else "false_rumor"
                if case_id.startswith("false-")
                else "positive",
                "endpoints": [
                    {
                        "endpoint_id": endpoint,
                        "expected_decision": "generic-correct-value",
                        "reference_time": "2020-01-03T00:00:00Z",
                        "time_precision": "second",
                    }
                    for endpoint in endpoints
                ],
            }
            for case_id in ids
        ],
    }
    keys = {
        "experiment_id": packet["experiment_id"],
        "cases": [{"case_id": case_id, "required_message_ids": ["public-1"]} for case_id in ids],
    }
    inputs = list(map(_bytes, (packet, rules, settings, answers, keys)))
    manifest = protocol.seal(*inputs, code_revision="frozen-offline-code")
    return manifest, inputs, endpoints


def _runs_and_reviews(manifest, inputs, endpoints):
    packet = json.loads(inputs[0])
    runs, reviews = [], {"cases": []}
    for arm in ("baseline", "query_planner"):
        run = {
            "experiment_id": manifest["experiment_id"],
            "started_at": manifest["sealed_at"],
            "arm": arm,
            "code_revision": manifest["code_revision"],
            **{
                key: manifest["commitments"][key]
                for key in ("packet_sha256", "rules_sha256", "model_settings_sha256")
            },
            "execution_receipt_sha256": "e" * 64,
            "cases": [
                {
                    "case_id": case["case_id"],
                    "used_message_ids": ["public-1"],
                    "report": {"status": "PASS", "text_sha256": "a" * 64},
                    "cost": {"model_calls": 1, "tokens": 10, "unknown_usage_calls": 0},
                    "actual_provider_versions": {"offline": "no-real-provider"},
                    "judgments": [
                        {
                            "kind": "judgment",
                            "judgment_id": endpoint + "-reviewed",
                            "endpoint_id": endpoint,
                            "decision": "generic-correct-value",
                            "as_of": "2020-01-02T00:00:00Z",
                            "evidence_message_ids": ["public-1"],
                        }
                        for endpoint in endpoints
                    ],
                    "alerts": [{"kind": "verify_now", "as_of": "2020-01-02T00:00:00Z"}],
                }
                for case in packet["cases"]
            ],
        }
        raw = _bytes(run)
        runs.append(raw)
        for case in run["cases"]:
            reviews["cases"].append(
                {
                    "run_sha256": protocol._hash(raw),
                    "case_id": case["case_id"],
                    "reviewer_role": "independent",
                    "public_only_execution_confirmed": True,
                    "text_sha256": case["report"]["text_sha256"],
                    "critical_facts_correct": True,
                    "corrections_applied": True,
                    "boundaries_preserved": True,
                    "inappropriate_alert": False,
                    "endpoints": [
                        {
                            "endpoint_id": endpoint,
                            "content_correct": True,
                            "earliest_correct_judgment_id": endpoint + "-reviewed",
                            "earlier_judgments_reviewed": True,
                        }
                        for endpoint in endpoints
                    ],
                }
            )
    return runs, reviews


def test_prepare_has_no_private_input_parameter_and_rejects_leak_or_changed_public_bytes(tmp_path):
    manifest, inputs, _ = _experiment()
    request = protocol.prepare(manifest, *inputs[:3], tmp_path / "blind")
    assert request["runtime_isolation"] == "not_yet_demonstrated"
    assert {path.name for path in (tmp_path / "blind").iterdir()} == {
        "packet.json",
        "rules.json",
        "model-settings.json",
        "run-request.json",
    }
    assert "answers_sha256" not in json.dumps(request)
    with pytest.raises(FileExistsError):
        protocol.prepare(manifest, *inputs[:3], tmp_path / "blind")
    with pytest.raises(ValueError, match="changed"):
        protocol.prepare(manifest, inputs[0] + b" ", *inputs[1:3], tmp_path / "changed")
    leaking = json.loads(inputs[0])
    leaking["cases"][0]["answers"] = "OFFLINE forbidden answer"
    with pytest.raises(ValueError, match="Private"):
        protocol.seal(_bytes(leaking), *inputs[1:], code_revision="frozen")
    with pytest.raises(ValueError, match="Duplicate"):
        protocol._load(b'{"id":1,"id":2}')


def test_grade_requires_matching_seal_paired_arms_and_independent_review():
    manifest, inputs, endpoints = _experiment()
    runs, reviews = _runs_and_reviews(manifest, inputs, endpoints)
    result = protocol.evaluate(manifest, *inputs, runs, reviews)
    assert result["acceptance"] == "passed"
    assert protocol.evaluate(manifest, *inputs, runs[:1], reviews)["acceptance"] != "passed"
    author_review = copy.deepcopy(reviews)
    for item in author_review["cases"]:
        item["reviewer_role"] = "author"
    author_result = protocol.evaluate(manifest, *inputs, runs, author_review)
    assert author_result["runs"][0]["true_case_recall"] == 0
    assert not author_result["runs"][0]["cases"][0]["endpoints"][0]["content_pass"]
    no_review = protocol.evaluate(manifest, *inputs, runs, {"cases": []})
    assert no_review["acceptance"] != "passed"
    assert "independent_content_and_blindness_review_missing" in no_review["runs"][0]["cases"][0]["issues"]
    preseal = json.loads(runs[0])
    preseal["started_at"] = "2000-01-01T00:00:00Z"
    bad = protocol.evaluate(manifest, *inputs, [_bytes(preseal), runs[1]], reviews)
    assert "run_started_before_seal" in bad["runs"][0]["issues"]
    with pytest.raises(ValueError, match="pre-run seal"):
        protocol.evaluate(manifest, *inputs[:3], inputs[3] + b" ", inputs[4], runs, reviews)


def test_D_content_endpoints_and_controls_are_required_not_only_an_early_alarm():
    manifest, inputs, endpoints = _experiment("D")
    runs, reviews = _runs_and_reviews(manifest, inputs, endpoints)
    result = protocol.evaluate(manifest, *inputs, runs, reviews)
    assert result["acceptance"] != "passed"
    assert "scenario_D_controls_insufficient" in result["runs"][0]["issues"]
    alarm_only = json.loads(runs[0])
    alarm_only["cases"][0]["judgments"] = []
    alarm_only["cases"][0]["alerts"] = [{"kind": "alert", "as_of": "2020-01-01T00:00:00Z"}]
    result = protocol.evaluate(manifest, *inputs, [_bytes(alarm_only), runs[1]], reviews)
    assert all(not endpoint["content_pass"] for endpoint in result["runs"][0]["cases"][0]["endpoints"])
    assert result["runs"][0]["true_case_recall"] == 0
    assert result["runs"][0]["first_supported_judgment_as_of"] is None


def test_D_finite_controls_do_not_call_worthwhile_verification_a_false_alarm():
    manifest, inputs, endpoints = _experiment("D", with_controls=True)
    runs, reviews = _runs_and_reviews(manifest, inputs, endpoints)
    result = protocol.evaluate(manifest, *inputs, runs, reviews)
    assert result["acceptance"] == "passed"
    control = result["runs"][0]["controls"]
    assert (
        control["case_count"] == 4 and control["false_rumor_count"] == 3 and control["same_season_count"] == 1
    )
    assert control["inappropriate_alert_cases"] == 0
    assert result["runs"][0]["cases"][-1]["verification_actions"] == 1
    assert "不估计稳定或可泛化" in control["qualification"]
    reviews["cases"][-1]["inappropriate_alert"] = True
    failed = protocol.evaluate(manifest, *inputs, runs, reviews)
    assert failed["acceptance"] != "passed"


def test_future_or_day_precision_and_unknown_cost_cannot_receive_a_complete_grade():
    manifest, inputs, endpoints = _experiment("D", with_controls=True)
    packet = json.loads(inputs[0])
    packet["cases"][0]["messages"][0].update(available_at="2020-01-02T00:00:00Z", time_precision="day")
    changed_inputs = [_bytes(packet), *inputs[1:]]
    manifest = protocol.seal(*changed_inputs, code_revision=manifest["code_revision"])
    runs, reviews = _runs_and_reviews(manifest, changed_inputs, endpoints)
    result = protocol.evaluate(manifest, *changed_inputs, runs, reviews)
    assert result["acceptance"] != "passed"
    assert (
        result["runs"][0]["cases"][0]["endpoints"][0]["timing_status"]
        == "availability_precision_insufficient"
    )
    future = copy.deepcopy(packet)
    future["cases"][0]["messages"][0].update(available_at="2020-01-04T00:00:00Z", time_precision="second")
    future_inputs = [_bytes(future), *inputs[1:]]
    future_manifest = protocol.seal(*future_inputs, code_revision=manifest["code_revision"])
    runs, reviews = _runs_and_reviews(future_manifest, future_inputs, endpoints)
    result = protocol.evaluate(future_manifest, *future_inputs, runs, reviews)
    assert result["runs"][0]["cases"][0]["endpoints"][0]["timing_status"] == "future_evidence"
    with pytest.raises(ValueError, match="timezone"):
        protocol._interval("2020-01-02", "day")


def test_independent_review_does_not_backdate_a_later_content_judgment():
    manifest, inputs, endpoints = _experiment("D", with_controls=True)
    runs, reviews = _runs_and_reviews(manifest, inputs, endpoints)
    changed = json.loads(runs[0])
    original_hash = protocol._hash(runs[0])
    changed["cases"][0]["judgments"].insert(
        0,
        {
            "kind": "judgment",
            "judgment_id": "earlier-not-approved",
            "endpoint_id": endpoints[0],
            "decision": "generic-correct-value",
            "as_of": "2020-01-01T00:00:00Z",
            "evidence_message_ids": ["public-1"],
        },
    )
    runs[0] = _bytes(changed)
    for item in reviews["cases"]:
        if item["run_sha256"] == original_hash:
            item["run_sha256"] = protocol._hash(runs[0])
    result = protocol.evaluate(manifest, *inputs, runs, reviews)
    assert (
        result["runs"][0]["cases"][0]["endpoints"][0]["first_correct_judgment_as_of"]
        == "2020-01-02T00:00:00Z"
    )
    changed["cases"][0]["cost"].update(tokens=None, unknown_usage_calls=1)
    original_hash = protocol._hash(runs[0])
    runs[0] = _bytes(changed)
    for item in reviews["cases"]:
        if item["run_sha256"] == original_hash:
            item["run_sha256"] = protocol._hash(runs[0])
    result = protocol.evaluate(manifest, *inputs, runs, reviews)
    assert "actual_cost_or_provider_receipts_incomplete" in result["runs"][0]["cases"][0]["issues"]
    assert result["acceptance"] != "passed"


def test_D_control_not_triggered_by_frozen_rule_does_not_require_an_invented_report():
    manifest, inputs, endpoints = _experiment("D", with_controls=True)
    answers = json.loads(inputs[3])
    answers["cases"][-1].update(expected_delivery="no_report", required_endpoint_ids=[], endpoints=[])
    inputs[3] = _bytes(answers)
    manifest = protocol.seal(*inputs, code_revision=manifest["code_revision"])
    runs, reviews = _runs_and_reviews(manifest, inputs, endpoints)
    for index, raw in enumerate(runs):
        run = json.loads(raw)
        control = run["cases"][-1]
        control.update(
            report=None,
            output_sha256="b" * 64,
            judgments=[],
            cost={"model_calls": 0, "tokens": 0, "unknown_usage_calls": 0},
        )
        runs[index] = _bytes(run)
        for item in reviews["cases"]:
            if item["run_sha256"] == protocol._hash(raw):
                item["run_sha256"] = protocol._hash(runs[index])
                if item["case_id"] == control["case_id"]:
                    item.update(no_report_expected_by_rule=True, output_sha256="b" * 64, endpoints=[])
    result = protocol.evaluate(manifest, *inputs, runs, reviews)
    assert result["acceptance"] == "passed"
    assert result["runs"][0]["cases"][-1]["report_assessment"] == "not_generated_by_frozen_rule"
    assert result["runs"][0]["true_case_recall"] == 1
    assert result["runs"][0]["first_supported_judgment_as_of"] == "2020-01-02T00:00:00Z"
    reviews["cases"][-1].pop("no_report_expected_by_rule")
    assert protocol.evaluate(manifest, *inputs, runs, reviews)["acceptance"] != "passed"


def test_duplicate_reviewed_judgment_id_cannot_backdate_the_content_endpoint():
    manifest, inputs, endpoints = _experiment("D", with_controls=True)
    runs, reviews = _runs_and_reviews(manifest, inputs, endpoints)
    actual = json.loads(runs[0])
    duplicate = copy.deepcopy(actual["cases"][0]["judgments"][0])
    duplicate["as_of"] = "2020-01-01T00:00:00Z"
    actual["cases"][0]["judgments"].insert(0, duplicate)
    prior_hash = protocol._hash(runs[0])
    runs[0] = _bytes(actual)
    for review in reviews["cases"]:
        if review["run_sha256"] == prior_hash:
            review["run_sha256"] = protocol._hash(runs[0])
    with pytest.raises(ValueError, match="judgment_id.*unique"):
        protocol.evaluate(manifest, *inputs, runs, reviews)
