"""SYNTHETIC method receipts only: loader isolation and lossless local/HTTP compatibility.

These opaque receipts exercise Radar passthrough, not BioCheck's semantic eligibility.
Actual frozen BioCheck compatibility is checked separately after upstream seals it.
"""

import copy
import hashlib
from types import SimpleNamespace

import pytest
from bioradar.adapters.biocheck import (
    _sources,
    audit_biocheck_support,
    check_biocheck,
    load_biocheck,
    load_biocheck_http,
    quality_feedback_biocheck,
    revise_biocheck,
    run_biocheck,
)

pytestmark = pytest.mark.BOOT
PURE_FILES = (
    "source_context.py",
    "evidence.py",
    "contracts.py",
    "prompts.py",
    "core.py",
    "claims.py",
    "rpc.py",
)
DRAFT = "SYNTHETIC: the method was not implemented [E1]. The study was registered [E1]."
REVISED = "SYNTHETIC: the method was not reported [E1]. The study was registered [E1]."
PROVIDER_VERSION = "SYNTHETIC-provider-contract-v2"


def no_model(*args, **kwargs):
    pytest.fail("Synthetic method receipt must not invoke a model")


def test_loader_executes_pure_dependencies_and_pins_protocol_version_inputs(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    sources = {
        "source_context.py": "TRACE = ['source_context']\n",
        "evidence.py": "from .source_context import TRACE\nTRACE.append('evidence')\n",
        "contracts.py": "from .evidence import TRACE\nTRACE.append('contracts')\nTOKEN = 'SYNTHETIC'\n",
        "prompts.py": "from .contracts import TOKEN\nfrom .evidence import TRACE\nTRACE.append('prompts')\n",
        "core.py": (
            "from .prompts import TOKEN\nfrom .evidence import TRACE\nTRACE.append('core')\n"
            "def generate(*args, **kwargs): return {'draft': TOKEN}\n"
            "def verify(*args, **kwargs): return {'status': 'SYNTHETIC'}\n"
            "def revise(*args, **kwargs): return {'draft': TOKEN}\n"
        ),
        "claims.py": (
            "from . import core\nfrom .core import TRACE\nTRACE.append('claims')\n"
            "def provider_version():\n from .rpc import PROVIDER_VERSION\n return PROVIDER_VERSION\n"
            "core.claim_provider_version = provider_version\n"
        ),
        "rpc.py": (
            "from .claims import TRACE\nTRACE.append('rpc')\nimport hashlib\nfrom pathlib import Path\n"
            "def method_version():\n return hashlib.sha256(b''.join((Path(__file__).parent/n).read_bytes() "
            "for n in ('analysis.py', 'service.py'))).hexdigest()\n"
            "PROVIDER_VERSION = method_version()\n"
        ),
    }
    for name, source in sources.items():
        (root / name).write_text(source)
    for name in ("__init__.py", "model.py", "analysis.py", "service.py"):
        (root / name).write_text("raise AssertionError('SYNTHETIC forbidden standalone module')\n")
    settings = SimpleNamespace(biocheck_project_dir=tmp_path)
    core, version = load_biocheck(settings)
    assert core is not None
    assert core.TRACE == [name.removesuffix(".py") for name in PURE_FILES]
    version_files = list(PURE_FILES) + ["analysis.py", "service.py"]
    assert list(_sources(settings)[0]) == version_files
    expected = hashlib.sha256(
        "".join(name + "\0" + (root / name).read_text() for name in version_files).encode()
    ).hexdigest()
    assert version == expected
    for name in ("analysis.py", "service.py"):
        old_provider = core.claim_provider_version()
        (root / name).write_text("raise AssertionError('SYNTHETIC version-only changed: " + name + "')\n")
        other, changed = load_biocheck(settings)
        assert other is not None and other is not core and changed != version
        assert other.TRACE == [name.removesuffix(".py") for name in PURE_FILES]
        assert other.claim_provider_version() != old_provider
        core, version = other, changed


def test_legacy_three_file_method_keeps_its_exact_version(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    sources = {
        "evidence.py": "SYNTHETIC = True\n",
        "prompts.py": "SYNTHETIC = True\n",
        "core.py": (
            "def generate(*args): return {}\ndef verify(*args): return {}\ndef revise(*args): return {}\n"
        ),
    }
    for name, source in sources.items():
        (root / name).write_text(source)
    core, version = load_biocheck(SimpleNamespace(biocheck_project_dir=tmp_path))
    assert core is not None
    assert (
        version
        == hashlib.sha256(
            "".join(name + "\0" + source for name, source in sources.items()).encode()
        ).hexdigest()
    )


def packet():
    return {
        "question": "SYNTHETIC method reporting status",
        "evidence": [{"id": "E1", "version": "SYNTHETIC-fixed-v1", "text": "SYNTHETIC source passage"}],
        "evidence_map": {"E1": {"version": "SYNTHETIC-fixed-v1"}},
    }


def verification():
    # Opaque controlled values are deliberately not interpreted or repaired by Radar.
    return {
        "status": "FAIL",
        "draft": DRAFT,
        "problem_units": ["U1"],
        "verification_errors": [],
        "verification_method": "cited_claims",
        "coverage": {"verdict": "complete"},
        "units": [
            {
                "unit_id": "U1",
                "start": 0,
                "end": len(DRAFT),
                "text": DRAFT,
                "issues": [],
                "claims": [
                    {
                        "kind": "fact",
                        "span": "the method was not implemented",
                        "verdict": "unsupported",
                        "repair_kind": "unreported_as_absent",
                        "repair_span": {"start": 15, "end": 29, "text": "not implemented"},
                        "evidence": [{"evidence_id": "E1", "version": "SYNTHETIC-fixed-v1"}],
                        "short_quotes": [
                            {
                                "kind": "source",
                                "evidence_id": "E1",
                                "segment_id": "S1",
                                "quote": "SYNTHETIC source passage",
                                "start": 0,
                                "end": 24,
                            },
                            {
                                "kind": "analysis_result",
                                "analysis_id": "A1",
                                "output_path": "/measurements/status",
                                "offset_basis": "selected_value_json",
                                "value_start": 0,
                                "value_end": 14,
                                "quote": '"not_reported"',
                            },
                        ],
                        "analysis_refs": [
                            {"analysis_id": "A1", "path": "/measurements/status"},
                            {"analysis_id": "A1", "path": "/output/measurements/status"},
                        ],
                        "numeric_resolutions": [
                            {"analysis_id": "A1", "output_path": "/measurements/status/value"}
                        ],
                    },
                    {
                        "kind": "fact",
                        "span": "The study was registered",
                        "verdict": "supported",
                        "evidence": [{"evidence_id": "E1", "version": "SYNTHETIC-fixed-v1"}],
                    },
                ],
            }
        ],
    }


class SyntheticMethods:
    provider_version = PROVIDER_VERSION

    def __init__(self):
        self.operations = []

    def generate(self, data, call, method="bound"):
        self.operations.append(("generate", method))
        return {"draft": DRAFT, "SYNTHETIC_generation_metadata": {"preserve": True}}

    def verify(self, data, draft, call, *, mode="whole", previous_review=None):
        self.operations.append(("verify", mode))
        assert draft == DRAFT
        return verification()

    def audit_support(self, data, review, call, *, targets=None):
        self.operations.append(("audit_support", copy.deepcopy(targets)))
        return {
            "status": "PASS",
            "original_status": "FAIL",
            "checks": [
                {
                    "unit_id": "U1",
                    "claim_index": 1,
                    "verdict": "supported",
                    "short_quotes": copy.deepcopy(review["units"][0]["claims"][0]["short_quotes"]),
                }
            ],
        }

    def quality_feedback(self, review, audit=None, *, source_errors=()):
        self.operations.append(("quality_feedback", None))
        return {
            "machine_status": "FAIL",
            "audit_status": "PASS",
            "evidence_gaps": [],
            "repair_units": ["U1"],
            "review_required": [],
            "repair_findings": [
                {
                    "repair_kind": "unreported_as_absent",
                    "repair_span": copy.deepcopy(review["units"][0]["claims"][0]["repair_span"]),
                }
            ],
        }

    def revise(self, data, review, call, *, audit=None, source_errors=()):
        self.operations.append(("revise", None))
        return {
            "draft": REVISED,
            "changed_units": [{"unit_id": "U1", "before": DRAFT, "after": REVISED}],
            "changed_claims": [{"unit_id": "U1", "claim_id": "C1", "repair_kind": "unreported_as_absent"}],
            "requires_verification": True,
            "verification_execution": {"SYNTHETIC": "not_rechecked"},
        }


def loaded(transport):
    methods = SyntheticMethods()
    if transport == "local":
        return (methods, PROVIDER_VERSION), methods

    def execute(operation, arguments, *, call=None):
        fn = getattr(methods, operation)
        args = copy.deepcopy(arguments)
        if operation != "quality_feedback":
            args["call"] = call
        return {
            "provider_version": PROVIDER_VERSION,
            "result": fn(**{{"packet": "data"}.get(key, key): value for key, value in args.items()}),
        }

    return load_biocheck_http(execute, PROVIDER_VERSION), methods


@pytest.mark.parametrize("transport", ["local", "http"])
def test_verification_new_fields_stay_lossless_and_mode_stays_paragraph(transport):
    binding, methods = loaded(transport)
    data = packet()
    before = copy.deepcopy(data)
    expected = verification()
    settings = SimpleNamespace(biocheck_project_dir=None)
    checked = check_biocheck(
        settings, data, DRAFT, no_model, repair=False, verification_mode="paragraph", loaded=binding
    )
    generated = run_biocheck(
        settings, data, no_model, repair=False, verification_mode="paragraph", loaded=binding
    )
    assert data == before
    for result in (checked, generated):
        assert result["verification"] == result["initial_verification"] == expected
        assert result["final_text"] == DRAFT and result["status"] == "FAIL"
        assert result["provider_version"] == PROVIDER_VERSION
    assert methods.operations == [("verify", "paragraph"), ("generate", "bound"), ("verify", "paragraph")]
    assert generated["generated"]["SYNTHETIC_generation_metadata"] == {"preserve": True}


@pytest.mark.parametrize("transport", ["local", "http"])
def test_audit_feedback_and_changed_claim_repair_kind_stay_lossless(transport):
    binding, _ = loaded(transport)
    settings = SimpleNamespace(biocheck_project_dir=None)
    data, review = packet(), verification()
    before = copy.deepcopy((data, review))
    audit = audit_biocheck_support(settings, data, review, no_model, loaded=binding)
    assert audit["status"] == "PASS"
    assert audit["checks"][0]["short_quotes"] == review["units"][0]["claims"][0]["short_quotes"]
    feedback = quality_feedback_biocheck(settings, data, review, audit=audit, loaded=binding)
    assert feedback["repair_findings"][0] == {
        "repair_kind": "unreported_as_absent",
        "repair_span": review["units"][0]["claims"][0]["repair_span"],
    }
    revised = revise_biocheck(settings, data, review, no_model, audit=audit, loaded=binding)
    assert (data, review) == before
    assert revised["status"] == "changed" and revised["draft"] == REVISED
    assert revised["changed_claims"] == [
        {"unit_id": "U1", "claim_id": "C1", "repair_kind": "unreported_as_absent"}
    ]
    assert revised["verification_execution"] == {"SYNTHETIC": "not_rechecked"}
    assert revised["needs_verification"] is revised["requires_verification"] is True
    assert revised["verification"] is revised["support_audit"] is None
