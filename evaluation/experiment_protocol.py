"""Seal, prepare public-only inputs, and grade separately supplied experiment records.

No command calls a model or reads private answers during blind preparation. The
execution environment must separately enforce the public-only mount and attest it;
creating a directory alone is not proof of blindness. Grading is a distinct command.
"""

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "bioradar-experiment-protocol-v1"
_PRIVATE_KEYS = {
    "answers",
    "answer_key",
    "expected_answer",
    "expected_label",
    "expected_decision",
    "gold",
    "gold_labels",
    "oracle",
    "private_notes",
    "key_messages",
    "required_message_ids",
}
_SHA = re.compile(r"[0-9a-f]{64}")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Inputs must be regular files, not symlinks")
    return path.read_bytes()


def _duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _load(data):
    return json.loads(data, object_pairs_hook=_duplicates)


def _write(path, value):
    with Path(path).open("x") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _public(value):
    if isinstance(value, dict):
        if any(str(key).lower() in _PRIVATE_KEYS for key in value):
            raise ValueError("Private answer or key-message fields are forbidden in blind inputs")
        for item in value.values():
            _public(item)
    elif isinstance(value, list):
        for item in value:
            _public(item)


def _identity(packet, rules):
    if not isinstance(packet, dict) or not isinstance(rules, dict):
        raise ValueError("Packet and rules must be JSON objects")
    if packet.get("experiment_id") != rules.get("experiment_id") or not packet.get("experiment_id"):
        raise ValueError("Experiment identity mismatch")
    if packet.get("scenario") not in {"A", "B", "C", "D"} or packet.get("scenario") != rules.get("scenario"):
        raise ValueError("Unknown or mismatched scenario")
    endpoints = rules.get("endpoint_ids")
    if not isinstance(endpoints, list) or not endpoints or len(set(endpoints)) != len(endpoints):
        raise ValueError("Endpoint IDs must be frozen and unique")
    if packet["scenario"] == "D" and len(endpoints) < 3:
        raise ValueError("Scenario D must separately evaluate at least three declared content endpoints")
    cases = packet.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Public packet needs cases")
    ids = [case.get("case_id") for case in cases]
    if not all(isinstance(item, str) and item for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("Case IDs must be unique")
    for case in cases:
        messages = case.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Every case needs public messages")
        msg_ids = [item.get("message_id") for item in messages]
        if not all(isinstance(item, str) and item for item in msg_ids) or len(set(msg_ids)) != len(msg_ids):
            raise ValueError("Message IDs must be unique within each case")


def seal(packet, rules, settings, answers, key_messages, code_revision):
    """Coordinator operation: hash answer bytes without exposing their contents."""
    public_packet, public_rules, public_settings = _load(packet), _load(rules), _load(settings)
    for item in (public_packet, public_rules, public_settings):
        _public(item)
    _identity(public_packet, public_rules)
    if not code_revision:
        raise ValueError("Frozen code revision is required")
    return {
        "schema_version": VERSION,
        "experiment_id": public_packet["experiment_id"],
        "scenario": public_packet["scenario"],
        "sealed_at": datetime.now(timezone.utc).isoformat(),
        "code_revision": code_revision,
        "commitments": {
            "packet_sha256": _hash(packet),
            "rules_sha256": _hash(rules),
            "model_settings_sha256": _hash(settings),
            "answers_sha256": _hash(answers),
            "key_messages_sha256": _hash(key_messages),
        },
    }


def prepare(manifest, packet, rules, settings, output):
    """Only public inputs are accepted here; private files are not parameters."""
    if manifest.get("schema_version") != VERSION:
        raise ValueError("Unknown seal version")
    for name, data in (("packet", packet), ("rules", rules), ("model_settings", settings)):
        if _hash(data) != manifest["commitments"][name + "_sha256"]:
            raise ValueError("Public input changed after sealing")
        _public(_load(data))
    _identity(_load(packet), _load(rules))
    folder = Path(output)
    folder.mkdir(exist_ok=False)
    for name, data in (("packet.json", packet), ("rules.json", rules), ("model-settings.json", settings)):
        with (folder / name).open("xb") as handle:
            handle.write(data)
    request = {
        "schema_version": VERSION,
        "experiment_id": manifest["experiment_id"],
        "scenario": manifest["scenario"],
        "code_revision": manifest["code_revision"],
        "packet_sha256": _hash(packet),
        "rules_sha256": _hash(rules),
        "model_settings_sha256": _hash(settings),
        "arms": ["baseline", "query_planner"],
        "runtime_isolation": "not_yet_demonstrated",
        "qualification": (
            "这里只准备公开输入。运行环境须另行强制只挂载该目录并封存隔离回执；"
            "本工具不证明外部进程无法读取私有文件。"
        ),
    }
    _write(folder / "run-request.json", request)
    return request


def _interval(value, precision):
    if not isinstance(value, str):
        raise ValueError("Missing actual availability time")
    start = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if precision == "day":
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    if start.tzinfo is None:
        raise ValueError("Availability timezone is not established; do not invent an hour")
    width = {"day": timedelta(days=1), "minute": timedelta(minutes=1), "second": timedelta(0)}.get(precision)
    if width is None:
        raise ValueError("Availability precision is not established")
    return start, start + width


def _review_for(reviews, run_hash, case_id):
    return next(
        (
            item
            for item in reviews.get("cases", [])
            if item.get("run_sha256") == run_hash and item.get("case_id") == case_id
        ),
        {},
    )


def evaluate(manifest, packet, rules, settings, answers, key_messages, runs, reviews):
    """Compare frozen records; semantic judgments come only from separate independent review."""
    for name, data in (
        ("packet", packet),
        ("rules", rules),
        ("model_settings", settings),
        ("answers", answers),
        ("key_messages", key_messages),
    ):
        if _hash(data) != manifest["commitments"][name + "_sha256"]:
            raise ValueError("Grading input differs from its pre-run seal")
    packet, rules, answers, key_messages = map(_load, (packet, rules, answers, key_messages))
    _identity(packet, rules)
    if (
        answers.get("experiment_id") != manifest["experiment_id"]
        or key_messages.get("experiment_id") != manifest["experiment_id"]
    ):
        raise ValueError("Private grading identity mismatch")
    packet_cases = {item["case_id"]: item for item in packet["cases"]}
    expected = {item["case_id"]: item for item in answers.get("cases", [])}
    required = {
        item["case_id"]: item.get("required_message_ids", []) for item in key_messages.get("cases", [])
    }
    if set(expected) != set(packet_cases) or set(required) != set(packet_cases):
        raise ValueError("Answers and key-message checklist must cover the sealed cases exactly")
    outputs, arms = [], set()
    for raw in runs:
        run, run_hash = _load(raw), _hash(raw)
        arm, issues = run.get("arm"), []
        if arm not in {"baseline", "query_planner"}:
            raise ValueError("Unknown comparison arm")
        arms.add(arm)
        try:
            started, _ = _interval(run.get("started_at"), "second")
            sealed_at, _ = _interval(manifest.get("sealed_at"), "second")
            if started < sealed_at:
                issues.append("run_started_before_seal")
        except (ValueError, TypeError):
            issues.append("run_start_time_not_established")
        for name in ("packet_sha256", "rules_sha256", "model_settings_sha256"):
            if run.get(name) != manifest["commitments"][name]:
                issues.append("run_" + name + "_mismatch")
        if run.get("code_revision") != manifest["code_revision"]:
            issues.append("code_revision_mismatch")
        if run.get("experiment_id") != manifest["experiment_id"]:
            issues.append("experiment_identity_mismatch")
        receipt = run.get("execution_receipt_sha256")
        if not isinstance(receipt, str) or not _SHA.fullmatch(receipt):
            issues.append("runtime_isolation_receipt_missing")
        run_cases = {item["case_id"]: item for item in run.get("cases", [])}
        if set(run_cases) != set(packet_cases):
            issues.append("run_case_set_incomplete")
        cases = []
        for case_id, case in packet_cases.items():
            actual, oracle = run_cases.get(case_id, {}), expected[case_id]
            judgments = actual.get("judgments", [])
            if not isinstance(judgments, list) or any(not isinstance(item, dict) for item in judgments):
                raise ValueError("Case judgments must be a list of records")
            judgment_ids = [item.get("judgment_id") for item in judgments if item.get("kind") == "judgment"]
            if any(not isinstance(identity, str) or not identity for identity in judgment_ids) or len(
                set(judgment_ids)
            ) != len(judgment_ids):
                raise ValueError("Every judgment_id must be nonempty and unique within a case")
            review = _review_for(reviews, run_hash, case_id)
            case_issues, endpoints = [], []
            independent = (
                review.get("reviewer_role") == "independent"
                and review.get("public_only_execution_confirmed") is True
            )
            if not independent:
                case_issues.append("independent_content_and_blindness_review_missing")
            report = actual.get("report") or {}
            is_control = oracle.get("control_role") in {"same_season", "false_rumor"}
            no_report = is_control and oracle.get("expected_delivery") == "no_report" and not report
            if no_report:
                if review.get("no_report_expected_by_rule") is not True:
                    case_issues.append("absence_of_report_not_independently_confirmed_by_frozen_rule")
                digest = actual.get("output_sha256")
                if not isinstance(digest, str) or not _SHA.fullmatch(digest):
                    case_issues.append("control_output_hash_missing")
                if review.get("output_sha256") != digest:
                    case_issues.append("independent_review_output_mismatch")
            else:
                if report.get("status") != "PASS":
                    case_issues.append("report_text_verification_not_pass")
                if not isinstance(report.get("text_sha256"), str) or not _SHA.fullmatch(
                    report["text_sha256"]
                ):
                    case_issues.append("report_body_hash_missing")
                if review.get("text_sha256") != report.get("text_sha256"):
                    case_issues.append("independent_review_body_mismatch")
            reviewed_hash = review.get("output_sha256" if no_report else "text_sha256")
            actual_hash = actual.get("output_sha256") if no_report else report.get("text_sha256")
            review_bound = (
                independent
                and isinstance(actual_hash, str)
                and bool(_SHA.fullmatch(actual_hash))
                and reviewed_hash == actual_hash
            )
            for criterion in ("critical_facts_correct", "corrections_applied", "boundaries_preserved"):
                if review.get(criterion) is not True:
                    case_issues.append("independent_" + criterion + "_not_pass")
            used = set(actual.get("used_message_ids", []))
            message_ids = {item["message_id"] for item in case["messages"]}
            if not used <= message_ids:
                case_issues.append("unknown_or_cross_case_message_reference")
            critical = set(required[case_id])
            if not critical <= message_ids:
                raise ValueError("Sealed key-message checklist contains a non-public message")
            hits = len(critical & used)
            if critical - used:
                case_issues.append("critical_messages_missing")
            messages = {item["message_id"]: item for item in case["messages"]}
            expected_endpoints = {item["endpoint_id"]: item for item in oracle.get("endpoints", [])}
            required_endpoints = oracle.get("required_endpoint_ids", rules["endpoint_ids"])
            if not isinstance(required_endpoints, list) or len(set(required_endpoints)) != len(
                required_endpoints
            ):
                raise ValueError("Invalid frozen per-case endpoint set")
            if not set(required_endpoints) <= set(rules["endpoint_ids"]):
                raise ValueError("Per-case endpoint was not frozen in the public rules")
            if not is_control and set(required_endpoints) != set(rules["endpoint_ids"]):
                raise ValueError("Primary cases must grade all frozen content endpoints")
            if set(expected_endpoints) != set(required_endpoints):
                raise ValueError("Private endpoint expectations must match the frozen per-case set")
            endpoint_reviews = {item["endpoint_id"]: item for item in review.get("endpoints", [])}
            for endpoint_id in required_endpoints:
                reference = expected_endpoints[endpoint_id]
                judgments = [
                    item
                    for item in actual.get("judgments", [])
                    if item.get("endpoint_id") == endpoint_id and item.get("kind") == "judgment"
                ]
                endpoint_review = endpoint_reviews.get(endpoint_id, {})
                reviewed_id = endpoint_review.get("earliest_correct_judgment_id")
                content_ok = endpoint_review.get("content_correct") is True
                correct = [
                    item
                    for item in judgments
                    if reviewed_id
                    and item.get("judgment_id") == reviewed_id
                    and item.get("decision") == reference.get("expected_decision")
                ]
                if (
                    manifest["scenario"] == "D"
                    and endpoint_review.get("earlier_judgments_reviewed") is not True
                ):
                    case_issues.append("earlier_judgments_not_independently_reviewed:" + endpoint_id)
                first, timing, lead_seconds, lead_days = None, "unknown", None, None
                for judgment in sorted(correct, key=lambda item: str(item.get("as_of", ""))):
                    refs = judgment.get("evidence_message_ids", [])
                    if not refs or not set(refs) <= used:
                        timing = "referenced_evidence_not_actually_used"
                        continue
                    try:
                        cutoff, _ = _interval(judgment.get("as_of"), "second")
                        intervals = [
                            _interval(messages[key].get("available_at"), messages[key].get("time_precision"))
                            for key in refs
                        ]
                        if not intervals:
                            continue
                        if any(start > cutoff for start, _ in intervals):
                            timing = "future_evidence"
                            continue
                        if any(end > cutoff for _, end in intervals):
                            timing = "availability_precision_insufficient"
                            continue
                        ref_start, ref_end = _interval(
                            reference.get("reference_time"), reference.get("time_precision")
                        )
                        first, timing = judgment["as_of"], "established"
                        if reference.get("time_precision") == "second":
                            lead_seconds = (ref_start - cutoff).total_seconds()
                        else:
                            lead_days = {
                                "lower": (ref_start - cutoff).total_seconds() / 86400,
                                "upper": (ref_end - cutoff).total_seconds() / 86400,
                            }
                        break
                    except (ValueError, KeyError, TypeError):
                        continue
                supported = any(
                    item.get("evidence_message_ids")
                    and set(item["evidence_message_ids"]) <= used & message_ids
                    for item in correct
                )
                endpoint_ok = bool(correct) and content_ok and review_bound and supported
                if not endpoint_ok:
                    case_issues.append("endpoint_content_not_pass:" + endpoint_id)
                if timing == "future_evidence":
                    case_issues.append("future_evidence_used:" + endpoint_id)
                if manifest["scenario"] == "D" and timing != "established":
                    case_issues.append("endpoint_timing_not_established:" + endpoint_id)
                endpoints.append(
                    {
                        "endpoint_id": endpoint_id,
                        "content_pass": endpoint_ok,
                        "first_correct_judgment_as_of": first,
                        "timing_status": timing,
                        "lead_seconds": lead_seconds,
                        "lead_days": lead_days,
                    }
                )
            cost = actual.get("cost", {})
            if (
                type(cost.get("model_calls")) is not int
                or cost["model_calls"] < 0
                or type(cost.get("tokens")) is not int
                or cost["tokens"] < 0
                or type(cost.get("unknown_usage_calls")) is not int
                or cost["unknown_usage_calls"] != 0
                or not isinstance(actual.get("actual_provider_versions"), dict)
                or not actual["actual_provider_versions"]
            ):
                case_issues.append("actual_cost_or_provider_receipts_incomplete")
            actions = actual.get("alerts", [])
            alert_count = sum(item.get("kind") == "alert" for item in actions)
            false_alert = review.get("inappropriate_alert")
            if oracle.get("control_role") in {"same_season", "false_rumor"} and type(false_alert) is not bool:
                case_issues.append("independent_control_alarm_review_missing")
            if false_alert is True and not alert_count:
                case_issues.append("false_alarm_review_has_no_actual_alert")
            cases.append(
                {
                    "case_id": case_id,
                    "issues": case_issues,
                    "endpoints": endpoints,
                    "key_messages_used": hits,
                    "key_messages_required": len(critical),
                    "key_message_recall": hits / len(critical) if critical else None,
                    "alert_count": alert_count,
                    "verification_actions": sum(item.get("kind") == "verify_now" for item in actions),
                    "first_alert_as_of": next(
                        (item.get("as_of") for item in actions if item.get("kind") == "alert"), None
                    ),
                    "inappropriate_alert": false_alert,
                    "control_role": oracle.get("control_role"),
                    "cost": cost,
                    "report_assessment": "not_generated_by_frozen_rule"
                    if no_report
                    else report.get("status"),
                    "actual_provider_versions": actual.get("actual_provider_versions"),
                    "acceptance": "passed" if not case_issues else "incomplete_or_failed",
                }
            )
        controls = [item for item in cases if item["control_role"] in {"same_season", "false_rumor"}]
        false_rumors = sum(item["control_role"] == "false_rumor" for item in controls)
        seasonal = sum(item["control_role"] == "same_season" for item in controls)
        false_alarms = sum(item["inappropriate_alert"] is True for item in controls)
        if manifest["scenario"] == "D":
            if false_rumors < 3 or seasonal < 1:
                issues.append("scenario_D_controls_insufficient")
            threshold = rules.get("max_inappropriate_control_alerts")
            if type(threshold) is not int or threshold < 0 or not rules.get("false_alarm_definition"):
                issues.append("false_alarm_rule_not_preregistered")
            elif false_alarms > threshold:
                issues.append("preregistered_false_alarm_limit_exceeded")
        primary = [item for item in cases if item["control_role"] not in {"same_season", "false_rumor"}]
        if not primary:
            issues.append("no_primary_case_cannot_pass_by_silence")
        recalled = [
            item
            for item in primary
            if item["endpoints"]
            and all(
                endpoint["content_pass"]
                and (manifest["scenario"] != "D" or endpoint["timing_status"] == "established")
                for endpoint in item["endpoints"]
            )
        ]
        supported_times = [
            endpoint["first_correct_judgment_as_of"]
            for item in primary
            for endpoint in item["endpoints"]
            if endpoint["content_pass"] and endpoint["timing_status"] == "established"
        ]
        outputs.append(
            {
                "arm": arm,
                "run_sha256": run_hash,
                "true_case_count": len(primary),
                "true_cases_with_supported_judgment": len(recalled),
                "true_case_recall": len(recalled) / len(primary) if primary else None,
                "first_supported_judgment_as_of": min(
                    supported_times, key=lambda value: _interval(value, "second")[0]
                )
                if supported_times
                else None,
                "issues": issues,
                "cases": cases,
                "controls": {
                    "case_count": len(controls),
                    "same_season_count": seasonal,
                    "false_rumor_count": false_rumors,
                    "inappropriate_alert_cases": false_alarms,
                    "qualification": (
                        "只描述这些有限对照及分母，不估计稳定或可泛化的误报率；"
                        "核实行动不因事后传言为假而自动算误报。"
                    ),
                },
            }
        )
    complete = arms == {"baseline", "query_planner"} and len(outputs) == 2
    passed = complete and all(
        not run["issues"] and all(case["acceptance"] == "passed" for case in run["cases"]) for run in outputs
    )
    return {
        "schema_version": VERSION,
        "experiment_id": manifest["experiment_id"],
        "scenario": manifest["scenario"],
        "paired_comparison_complete": complete,
        "acceptance": "passed" if passed else "incomplete_or_failed",
        "runs": outputs,
        "qualification": (
            "首次报警只是参考，内容判断和可见时间分别评估。现代模型可能含训练后见知识；"
            "历史回放不是当时的真实预测。工程检查通过不替代独立科研评测。"
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="phase", required=True)
    for name in ("seal", "prepare", "grade"):
        command = commands.add_parser(name)
        for argument in ("packet", "rules", "model-settings", "output"):
            command.add_argument("--" + argument, required=True)
        if name == "seal":
            command.add_argument("--code-revision", required=True)
        else:
            command.add_argument("--seal", required=True)
        if name in {"seal", "grade"}:
            command.add_argument("--answers", required=True)
            command.add_argument("--key-messages", required=True)
        if name == "grade":
            command.add_argument("--run", action="append", required=True)
            command.add_argument("--reviews", required=True)
    args = parser.parse_args(argv)
    public = [_read(getattr(args, name)) for name in ("packet", "rules", "model_settings")]
    if args.phase == "seal":
        result = seal(*public, _read(args.answers), _read(args.key_messages), args.code_revision)
    else:
        manifest = _load(_read(args.seal))
        if args.phase == "prepare":
            result = prepare(manifest, *public, args.output)
            print(json.dumps(result, ensure_ascii=False))
            return
        result = evaluate(
            manifest,
            *public,
            _read(args.answers),
            _read(args.key_messages),
            [_read(path) for path in args.run],
            _load(_read(args.reviews)),
        )
    _write(args.output, result)
    print(
        json.dumps(
            {
                "schema_version": VERSION,
                "phase": args.phase,
                "output": str(args.output),
                "acceptance": result.get("acceptance", "sealed"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
