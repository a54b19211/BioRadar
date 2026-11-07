"""Why the mechanism stage did not run is recorded as a code, and the external views keep the code."""

import pytest
from test_access import admin_client, fabric_sources, topic_analysis  # noqa: F401 - the fixture a test here takes

from bioradar import app, db
from bioradar.config import settings
from bioradar.research import analyses, views
from bioradar.research.pipeline import Run
from bioradar.research.state import MECH_NOT_RUN_REASONS

PLAN = {"statement": "X 抑制 Y"}
MECH_URL = "http://mech.invalid"


@pytest.mark.parametrize(
    ("target", "url", "reason"),
    [
        (None, MECH_URL, "no_mechanism_question"),
        (PLAN, "", "not_configured"),
        (PLAN, MECH_URL, "no_research_material"),  # the only record read is a notice, not research material
    ],
)
def test_a_mechanism_stage_that_does_not_run_records_the_actual_reason(target, url, reason):
    row = analyses.create(question="X 是否抑制 Y？", direction="science", is_demo=True, enqueue=False)
    run = Run(row, settings().model_copy(update={"mechevidence_url": url}), fabric=None)
    run.p.update(facts=[{"ref": "E1"}], mechanism=target)
    with db.session() as s:
        s.add(
            db.Evidence(
                analysis_id=row.id,
                ref="E1",
                source="external_archive",
                record_id="N1",
                version_id="v" * 64,
                title="通报",
                material_kind="official_notice",
                verified=True,
                text="通报全文",
            )
        )
    run.mechanism()
    assert run.p["mechanism_result"] == {"status": "not_run", "reason": reason}
    assert reason in MECH_NOT_RUN_REASONS


def test_the_reason_of_a_mechanism_not_run_stays_when_text_is_withheld(fabric_sources):  # noqa: F811
    admin = admin_client()
    analysis_id, _ = topic_analysis()  # cites gone_src, which Fabric no longer lists: its words are withheld
    kept = {"status": "not_run", "reason": "no_research_material"}
    with db.session() as s:
        row = s.get(db.Analysis, analysis_id)
        row.result = {**row.result, "mechanism": kept}
    result = admin.get(f"/api/analyses/{analysis_id}").json()["result"]
    assert result["withheld"] and result["mechanism"] == kept
    # the other exit that withholds text keeps it too, and a reason that is not a code is still text
    assert app.withhold({"mechanism": kept}) == {"mechanism": kept}
    assert views.blank({"reason": "据撤权原文"}) == {"reason": ""}
    assert app.withhold({"reason": "据撤权原文"}) == {"reason": ""}
