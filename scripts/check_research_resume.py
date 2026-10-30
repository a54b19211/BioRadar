"""Offline controls for resuming the same accepted research without another first ask."""

from __future__ import annotations

import copy
import json
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import research_e2e as module


class Secret:
    def __init__(self, value):
        self.value = value

    def get_secret_value(self):
        return self.value


def arguments(**values):
    return Namespace(
        **dict(
            question=None, followup=None, source=[], policy=None, case_profile=None, require_mechanism=None
        )
        | values
    )


def offline_flow(exercise, inputs):
    first = {
        "id": "offline-first",
        "investigation_id": "offline-investigation",
        "question": "SYNTHETIC fixed original question",
        "parent_analysis_id": None,
        "evidence": [],
        "queries": [],
        "query_policy": {"offline": True},
        "calls": [
            {"stage": stage, "receipt": {"status": "completed"}}
            for stage in ("mech_decomposition", "mech_local_review", "mech_mechanism_review")
        ],
        "result": {"report": {"status": "PASS"}, "mechanism_evaluation": {"status": "completed"}},
    }
    second = dict(first, id="offline-followup", question=inputs["followup"], parent_analysis_id=first["id"])
    requests, waited, reports = [], [], []

    def call(method, path, *, body=None, key=None):
        requests.append((method, path, copy.deepcopy(body), key))
        if method == "POST":
            assert path.endswith("/offline-investigation/analyses"), "Resume must never resend the first ask"
            assert exercise.state["followup_request"] == {"body": body, "key": key}
            assert "budget" not in body
            value = second
        elif path.endswith("/research-options"):
            value = {
                "runtime": {
                    "environment": "test",
                    "database_name": exercise.expected_database,
                    "workspace_id": exercise.expected_workspace,
                }
            }
        elif path.endswith("/analyses/offline-first"):
            value = first
        else:
            assert path.endswith("/investigations/offline-investigation")
            value = {"revision": 7}
        return SimpleNamespace(json=lambda: copy.deepcopy(value))

    def wait(investigation_id, analysis_id):
        waited.append((investigation_id, analysis_id))
        return copy.deepcopy(first if analysis_id == first["id"] else second)

    def report(view):
        reports.append(view["id"])
        exercise.state.setdefault("policy_traces", {})[view["id"]] = {
            "status": "insufficient",
            "unsearched_checks": ["offline-unchecked-scope"],
        }

    exercise.call, exercise.wait, exercise.check_report = call, wait, report
    with redirect_stdout(StringIO()):
        exercise.run(**{key: value for key, value in inputs.items() if key not in {"policy", "case_profile"}})
    assert waited == [
        ("offline-investigation", "offline-first"),
        ("offline-investigation", "offline-followup"),
    ]
    assert reports == ["offline-first", "offline-followup"]
    assert len([item for item in requests if item[0] == "POST"]) == 1
    assert exercise.state["prior_analysis_ids"] == ["offline-first"]
    assert exercise.state["status"] == "completed_with_unchecked_scope"
    return requests


def main():
    settings = SimpleNamespace(
        environment="test",
        database_url=Secret("postgresql://offline/bioradar_research_offline"),
        engine_workspace_id="offline-workspace",
        engine_dev_credential=Secret("SYNTHETIC-LOCAL-ONLY"),
    )
    prior = {
        "run": "offline-previous-run",
        "case_profile": "pipeline_layout",
        "requested_policy": "query_planner",
        "status": "failed",
        "created": {
            "analysis_id": "offline-first",
            "investigation_id": "offline-investigation",
            "analysis": {
                "question": "SYNTHETIC fixed original question",
                "search_scope": ["clinicaltrials", "europe_pmc"],
            },
        },
        "analyses": {"offline-first": {"state": "blocked"}},
    }
    with (
        TemporaryDirectory(prefix="bioradar-research-resume-") as directory,
        patch.object(module, "Settings", return_value=settings),
    ):
        old = Path(directory) / "old.json"
        old.write_text(json.dumps(prior))
        frozen = old.read_bytes()
        loaded = module.read_resume(old)
        inputs = module.run_inputs(arguments(require_mechanism=True), loaded)
        assert inputs["question"] == prior["created"]["analysis"]["question"]
        assert inputs["sources"] == ["clinicaltrials", "europe_pmc"]
        assert inputs["case_profile"] == "pipeline_layout" and inputs["require_mechanism"] is True
        assert inputs["require_mechanism_basis"] == "explicit_cli"
        output = Path(directory) / "new.json"
        exercise = module.ResearchExercise(
            "http://127.0.0.1:1",
            output,
            policy=inputs["policy"],
            case_profile=inputs["case_profile"],
            resume_from=old,
            prior=loaded,
        )
        try:
            requests = offline_flow(exercise, inputs)
            assert [item for item in requests if item[0] == "POST"][0][3] == "offline-previous-run-followup"
            assert exercise.state["resume_from"] == str(old.resolve())
            assert old.read_bytes() == frozen
        finally:
            exercise.client.close()
        try:
            module.ResearchExercise("http://127.0.0.1:1", output)
        except FileExistsError:
            pass
        else:
            raise AssertionError("Output must retain exclusive creation")
        try:
            module.run_inputs(arguments(question="changed question"), loaded)
        except ValueError:
            pass
        else:
            raise AssertionError("Resume changed the original question")
        invalid = Path(directory) / "missing.json"
        invalid.write_text("{}")
        try:
            module.read_resume(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("Resume accepted missing old IDs")
    print(
        json.dumps(
            {
                "status": "passed",
                "checks": [
                    "accepted_ids_and_inputs_retained",
                    "no_first_ask_or_task_state_write",
                    "one_followup_uses_shared_report_and_old_version_checks",
                    "followup_intent_saved_before_post",
                    "old_artifact_immutable_new_output_exclusive",
                    "changed_original_question_and_missing_ids_rejected",
                ],
                "qualification": "Offline flow controls only; no HTTP or model requests.",
            }
        )
    )


if __name__ == "__main__":
    main()
