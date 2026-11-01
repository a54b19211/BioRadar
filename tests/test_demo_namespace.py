"""Configured simulation identity must survive authorization and cannot label real data."""

import pytest
from bioradar.api.config import Settings
from bioradar.api.errors import DomainError
from bioradar.research.engine.auth import DevelopmentAccess
from bioradar.research.engine.service import EngineService
from test_engine_persistence import CREDENTIAL
from test_engine_persistence import snapshot as snapshot  # noqa: F401

pytestmark = pytest.mark.ENGINE


def named_snapshot(snapshot, namespace, *, is_demo=True):
    return snapshot.model_copy(
        update={
            "source_instance": namespace,
            "is_demo": is_demo,
            "records": tuple(r.model_copy(update={"source_instance": namespace}) for r in snapshot.records),
            "coverage": tuple(r.model_copy(update={"source_instance": namespace}) for r in snapshot.coverage),
        }
    )


def test_explicit_demo_namespace_is_authorized_without_fixture_alias(snapshot):
    namespace = "explicit-simulation-namespace"
    settings = Settings(
        engine_dev_credential=CREDENTIAL,
        data_fabric_demo_source_instance=namespace,
        engine_source_allowlist=[namespace, "real-source"],
        engine_source_id_allowlist={namespace: ["fixture-source"], "real-source": ["fixture-source"]},
    )
    engine = EngineService(None, settings)
    actor = DevelopmentAccess(settings).actor()
    engine._authorize_snapshot(actor, named_snapshot(snapshot, namespace))
    for value in [
        named_snapshot(snapshot, namespace, is_demo=False),
        named_snapshot(snapshot, "real-source", is_demo=True),
    ]:
        with pytest.raises(DomainError) as error:
            engine._authorize_snapshot(actor, value)
        assert error.value.code == "fixture_scope_mismatch"
    with pytest.raises(DomainError) as error:
        engine._authorize_snapshot(actor, snapshot)
    assert error.value.code == "source_not_authorized"


def test_unconfigured_namespace_still_accepts_default_fixture(snapshot):
    settings = Settings(engine_dev_credential=CREDENTIAL)
    engine = EngineService(None, settings)
    engine._authorize_snapshot(DevelopmentAccess(settings).actor(), snapshot)
