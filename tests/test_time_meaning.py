"""Source times keep their meaning on the way into the radar (task book 01 2.4): a record without a published
time is not given its event time, and a GDELT observation time is not a modification of the source."""

from conftest import record
from test_registry_changes import trial

from bioradar.adapters.fabric import document
from bioradar.research.monitor import route


def test_a_missing_published_time_is_not_replaced_by_the_event_time():
    notice = record("W1", "WHO notice", "text")
    notice.pop("source_published_at")
    notice["event_time"] = {"value": "2026-10-02", "precision": "day", "meaning": "who_notice_publication"}

    doc = document(notice)

    assert doc["published_at"] is None
    assert doc["event_time"] == {"value": "2026-10-02", "meaning": "who_notice_publication"}


def test_the_published_time_and_the_event_time_each_carry_their_own_meaning():
    notice = record("W2", "WHO notice", "text")
    notice["source_published_at"] = {
        "value": "2026-10-01",
        "precision": "day",
        "meaning": "who_notice_publication",
    }
    notice["event_time"] = {"value": "2026-10-03", "precision": "day"}  # Fabric named no meaning

    doc = document(notice)

    assert doc["published_at"] == {"value": "2026-10-01", "meaning": "who_notice_publication"}
    assert doc["event_time"] == {"value": "2026-10-03", "meaning": None}


def test_a_gdelt_observation_time_is_not_a_recent_update_of_the_source():
    def routed(meaning):
        stopped = trial(status="TERMINATED")  # updated 2 days before it was first seen, registered long ago
        stopped["source_updated_at"]["meaning"] = meaning
        doc = document(stopped)
        doc["registry_diff"] = {"state": "first_adoption", "changes": []}
        return route(doc)

    assert routed("source_reported") is None  # stopped and updated within 7 days: worth triage
    assert routed("gdelt_first_observed") == "inventory_first_adoption"  # the time says nothing of the source
