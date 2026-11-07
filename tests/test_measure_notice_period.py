"""measure.v9: the real NMPA notice shape - a 发布时间 line above the body - is built and
checked by the real checker v16; measure.v8's period rule (extraction_normalize.v2) refused it before it was
ever sent."""

import pytest
from conftest import FakeFabric, record
from extraction_contract import normalize as norm
from sqlalchemy import select
from test_measure_checks import SRC, real_post

from bioradar import db
from bioradar.adapters import llm, services
from bioradar.research import monitor, schemas

pytestmark = pytest.mark.skipif(not SRC, reason="BIORADAR_TEST_BIOCHECK_SRC (a BioCheck checkout) is not set")
CALL = {"stage": "triage", "model": "fake", "finish_reason": "stop", "usage": {}, "seconds": 0, "error": None}
NOTICE = (
    "关于批准注册医疗器械产品的公告（2026年3月）（2026年第38号）\n发布时间：2026-04-13\n"
    "2026年3月，国家药监局共批准注册医疗器械产品272个。"
)
QUOTE = "2026年3月，国家药监局共批准注册医疗器械产品272个"


def test_a_notice_with_its_publication_date_above_the_body_is_built_and_checked(monkeypatch):
    at = NOTICE.index(QUOTE)
    assert not norm.period_span_whole(NOTICE, at, at + len("2026年3月"))  # measure.v8 refused it
    monkeypatch.setattr(services, "biocheck_extraction", lambda s, body: real_post(body))
    m = schemas.Measure(
        object_text="国家药监局",
        metric_text="批准注册医疗器械产品",
        value_text="272",
        unit_text="个",
        period_text="2026年3月",
        period="2026-03",
        period_type="interval",
        kind="flow",
        qualifier="exact",
        modality="actual",
        status="reported",
        quote=QUOTE,
    )

    def model(settings_, stage, system, payload, model_cls, **options):
        if stage == "extract":
            return schemas.Extraction(
                items=[schemas.ExtractItem(index=w["index"]) for w in payload["windows"]]
            ), dict(CALL, stage=stage)
        return schemas.Triage(
            items=[
                schemas.TriageItem(index=r["index"], new_topic="注册", alert="normal", measures=[m])
                for r in payload["records"]
            ]
        ), CALL

    monkeypatch.setattr(llm, "chat_json", model)
    monitor.tick(is_demo=True, fabric=FakeFabric([record("N1", "注册公告", NOTICE)], clean=True))
    with db.session() as s:
        [c] = s.scalars(select(db.MeasurementCandidate))
        assert c.status == "checked" and c.reason is None
        [check] = s.scalars(select(db.MeasurementCheck))
        assert (check.checker_version, check.status) == ("biocheck.extraction_rules.v16", "consistent")
        assert check.request["items"][0]["producer"]["version"] == "measure.v10"
        assert check.response["items"][0]["fields"]["period"]["result"] == "match"
        [p] = s.scalars(select(db.Measurement))
        assert (p.period, p.value_text) == ("2026-03", "272")
