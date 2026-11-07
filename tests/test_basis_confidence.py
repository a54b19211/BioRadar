"""The basis confidence of a judgment (research/run/assess.py `cap_basis`, task book 08 2.5): "not assessed"
is not a low, and only the checks the judgment itself depends on can cap it."""

from types import SimpleNamespace

from bioradar.research.run import assess

PROGRESS = {
    "checks": [
        {"id": "Q1", "status": "answered"},
        {"id": "Q2", "status": "open"},
        {"id": "Q3", "status": "conflict"},
    ],
    "facts": [
        {"ref": "E1", "checks": ["Q1"]},
        {"ref": "E2", "checks": ["Q2"]},
        {"ref": "E3", "checks": ["Q3"]},
    ],
}


def graded(*pairs):
    """Judgments grounded on the refs of each pair, the model having proposed the basis confidence."""
    return {"judgments": [{"grounds": grounds, "basis_confidence": proposed} for grounds, proposed in pairs]}


def basis(assessment):
    return [j["basis_confidence"] for j in assessment["judgments"]]


def test_a_gap_the_judgment_does_not_rest_on_does_not_cap_it_but_its_own_open_or_conflicting_check_does():
    assessment = graded((["E1"], "high"), (["E1", "E2"], "high"), (["E3"], "medium"), (["E1"], "low"))
    assess.cap_basis(SimpleNamespace(p=PROGRESS), assessment, True)
    # Q2 is open, but the first judgment does not depend on it; the next two do (open, in conflict)
    assert basis(assessment) == ["high", "low", "low", "low"]


def test_without_a_proposal_or_a_check_of_the_conclusions_the_basis_is_not_assessed_not_low():
    assessment = graded((["E1"], ""), (["E1"], "high"))
    assess.cap_basis(SimpleNamespace(p=PROGRESS), assessment, True)
    assert basis(assessment) == ["not_assessed", "high"]  # the model proposed none for the first

    unchecked = graded((["E1"], "high"), (["E2"], "low"))
    assess.cap_basis(SimpleNamespace(p=PROGRESS), unchecked, False)  # BioCheck did not answer
    assert basis(unchecked) == ["not_assessed", "not_assessed"]
