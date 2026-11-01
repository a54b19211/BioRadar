"""Synthetic negative controls for source-aware attention and public-clock replay."""

from datetime import UTC, datetime

import pytest
from bioradar.research.mining.methods_text import STOP, phrase_spans
from bioradar.research.mining.models import MiningPolicy, SignalContext, SourceRelation, canonical_hash
from bioradar.research.mining.prepare import analysis_interval, prepare, section_visible, window_for
from bioradar.research.mining.signals import assess_records, checkable_claims, source_groups
from test_mining import AS_OF, provenance, record, snapshot

pytestmark = pytest.mark.ENGINE


def test_chinese_noise_is_not_a_feature_but_anonymous_checkable_rumor_is_retained():
    assert phrase_spans("不是没有，目前公司表示我们需要进一步研究。", STOP) == {}
    text = "据匿名群聊截图称，北江医院收治7例不明原因肺炎；病原未证实。"
    phrases = phrase_spans(text, STOP)
    assert "不明原因肺炎" in phrases
    assert not {"不是", "没有", "公司", "目前"}.intersection(phrases)
    claims = checkable_claims(text)
    assert claims and "7例" in claims[0]["text"]
    row = record(
        "rumor",
        text,
        source_family=None,
        source_independence_status="unresolved",
        study_cluster_ref=None,
        study_independence_status="unresolved",
        signal_context=SignalContext(attribution="anonymous", claimed_origin="群聊截图"),
    )
    assessment = assess_records([row])
    assert assessment["automatic_investigation_allowed"]
    assert assessment["evidence_level"] == "unverified"
    assert assessment["importance"] == "medium"
    assert assessment["urgency"] == "soon"
    assert assessment["independent_study_count"] is None
    emerging = record(
        "new-combination",
        "CX-814与ZX-935联合用于新型递送体系，是否协同尚待验证。",
        source_family=None,
        source_independence_status="unresolved",
        study_cluster_ref=None,
        study_independence_status="unresolved",
    )
    proposal = assess_records([emerging])
    assert proposal["automatic_investigation_allowed"]
    assert proposal["evidence_level"] == "unverified" and proposal["independent_study_count"] is None


def test_reposts_do_not_count_as_independent_sources_and_deletion_does_not_raise_risk():
    first = record(
        "original",
        "北江医院收治7例不明原因肺炎。",
        source_family="hospital-a",
        study_cluster_ref=None,
        study_independence_status="unresolved",
    )
    repost = record(
        "repost",
        "转发：北江医院收治7例不明原因肺炎。",
        source_family="news-b",
        study_cluster_ref=None,
        study_independence_status="unresolved",
        signal_context=SignalContext(
            source_relations=(SourceRelation(kind="repost", target_ref=first.record_ref),)
        ),
    )
    assert len(source_groups([first, repost])["confirmed"]) == 1
    plain = assess_records([first, repost])
    deleted = assess_records([first, repost.model_copy(update={"record_flags": ("deleted",)})])
    assert plain["importance"] == deleted["importance"]
    assert plain["urgency"] == deleted["urgency"]
    assert plain["evidence_level"] == deleted["evidence_level"]
    assert deleted["environment_signals"][0]["qualification"] == "context_not_proof_of_claim_or_risk"
    second = record(
        "institution-two",
        "河西医院确认3例不明原因肺炎。",
        source_family="hospital-c",
        study_cluster_ref=None,
        study_independence_status="unresolved",
    )
    confirmed = assess_records([first, second])
    assert confirmed["importance"] == "medium" and confirmed["urgency"] == "soon"
    assert confirmed["independent_study_count"] is None
    severe = record("severe", "河西医院报告2名医护人员感染。", source_family="hospital-c")
    assert assess_records([first, severe])["urgency"] == "soon"  # Independence alone is not confirmation.
    unverified = severe.model_copy(update={"source_independence_status": "unresolved", "source_family": None})
    urgent_check = assess_records([unverified])
    assert urgent_check["importance"] == "medium" and urgent_check["evidence_level"] == "unverified"
    negated = record("negative", "河西医院未证实2名医护人员感染。", source_family="hospital-c")
    assert assess_records([first, negated])["urgency"] != "immediate"
    denial = record("denial", "河西医院辟谣2名医护人员感染的说法。", source_family="hospital-c")
    denied = assess_records([denial])
    assert denied["importance"] == "medium" and denied["potentially_serious"]
    assert any(row["kind"] == "reaction" for row in denied["negative_signals"])
    unknown_cause = record(
        "unknown-cause",
        "河西医院报告2名医护人员感染，病原尚未确认。",
        source_family=None,
        source_independence_status="unresolved",
    )
    assert assess_records([unknown_cause])["importance"] == "medium"
    spaced = "  北江医院收治7例不明原因肺炎。"
    claim = checkable_claims(spaced)[0]
    assert spaced[claim["start"] : claim["end"]] == claim["text"]


def test_public_reconstruction_uses_verified_latest_public_time_without_rewriting_collection_clocks():
    end = datetime(2019, 12, 30, 23, 59, 59, 999999, tzinfo=UTC)
    proof = {
        "verified_by": "fixture-reviewer",
        "verified_at": AS_OF.isoformat(),
        "uri": "https://example.invalid/public-archive",
        "sha256": "f" * 64,
        "available_at": {"value": "2019-12-30", "precision": "day"},
    }
    row = record(
        "historical",
        "北江医院收治7例不明原因肺炎。",
        published=datetime(2019, 12, 30, tzinfo=UTC),
        observed_at=AS_OF,
        first_seen_at=AS_OF,
        available_at=AS_OF,
        public_available_at=end,
        availability_proof=proof,
    )
    fixed = snapshot([row])
    before = canonical_hash(fixed)
    policy = MiningPolicy(
        as_of=end, time_mode="availability_replay", availability_basis="public_history_reconstruction"
    )
    assert len(prepare(fixed, policy).records) == 1
    undated_publication = row.model_copy(update={"publication_time_verified": False})
    assert window_for(undated_publication, policy) == "current"
    low, high = analysis_interval(undated_publication, policy)
    assert low == datetime(2019, 12, 30, tzinfo=UTC) and high > end
    earlier = policy.model_copy(update={"as_of": datetime(2019, 12, 30, 12, tzinfo=UTC)})
    assert not prepare(fixed, earlier).records
    operational = policy.model_copy(update={"availability_basis": "operational_replay"})
    assert not prepare(fixed, operational).records
    invalid = row.model_copy(update={"availability_proof": {**proof, "verified_by": None}})
    assert not prepare(snapshot([invalid]), policy).records
    assert not section_visible(provenance("learned-later", AS_OF), row, policy)
    assert canonical_hash(fixed) == before and row.observed_at == AS_OF
    assert not prepare(fixed, policy).coverage_complete
    historical = prepare(fixed, policy).records[0]
    assert historical.confirmed_study is False  # No pre-cutoff study mapping was provided.
    assessed = assess_records([historical.record], qualified_study_refs={row.record_ref.version_key: False})
    assert assessed["independent_study_count"] is None
    assert assessed["confirmed_independent_study_count"] == 0
    assert "availability_basis" not in MiningPolicy(as_of=AS_OF).model_dump(mode="json")
    old = record("old-wire", "Legacy wire")
    assert not {"public_available_at", "availability_proof", "signal_context"}.intersection(
        old.model_dump(mode="json")
    )


def test_shared_doi_pmid_intersection_merges_exposure_without_confirming_study():
    first = record(
        "article",
        "CX-893三期试验240名患者。",
        source_family="journal-a",
        signal_context=SignalContext(
            document_evidence={
                "source_independence": {"status": "same_origin", "shared_identifiers": ["doi:10.1000/shared"]}
            }
        ),
    )
    second = record(
        "report",
        "CX-893三期试验240名患者。",
        source_family="news-b",
        signal_context=SignalContext(
            document_evidence={
                "source_independence": {
                    "status": "unknown",
                    "shared_identifiers": ["doi:10.1000/shared", "pmid:123456"],
                }
            }
        ),
    )
    assert len(source_groups([first, second])["confirmed"]) == 1
    unknown = [
        r.model_copy(update={"source_family": None, "source_independence_status": "unresolved"})
        for r in [first, second]
    ]
    assert not source_groups(unknown)["confirmed"]
    assert len(source_groups(unknown)["unknown"]) == 1
