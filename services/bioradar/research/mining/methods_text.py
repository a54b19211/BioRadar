"""Open-vocabulary lexical, exposure and topic calculations on frozen text."""

from __future__ import annotations

import json
import math
import re
import unicodedata
import warnings
from collections import defaultdict
from datetime import timedelta
from itertools import combinations

import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.decomposition import NMF
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from bioradar.research.mining.common import candidate, result, unsupported
from bioradar.research.mining.prepare import (
    PreparedRecord,
    PreparedSnapshot,
    publication_interval,
    section_visible,
)
from bioradar.research.mining.temporal import batch_bursts, exposure_metrics

STOP = frozenset(
    "a an and are as at be been by can for from has have in is it its of on or that the their this "
    "to was were "
    "will with study studies trial trials clinical patients patient subjects subject group groups arm arms "
    "procedure procedures eligible eligibility criteria inclusion exclusion intervention interventions "
    "sponsor enrollment primary secondary outcome outcomes phase randomized randomised protocol".split()
)
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-/+][A-Za-z0-9]+)*")
HAN = re.compile(r"[\u3400-\u9fff]+")
SEGMENT = re.compile(r"[^.!?。！？\n]+")


def is_structured_json(text: str) -> bool:
    if not text.lstrip().startswith(("{", "[")):
        return False
    try:
        return isinstance(json.loads(text), (dict, list))
    except (ValueError, RecursionError):
        return bool(re.search(r'"[^"\n]+"\s*:', text))


def text_items(prepared: PreparedSnapshot) -> tuple[list[tuple[PreparedRecord, str]], int]:
    remaining = prepared.policy.limits.max_text_characters
    selected = []
    truncated = 0
    for item in prepared.records:
        record = item.record
        if record.text_status != "inline_authorized" or not record.normalized_text:
            continue
        if is_structured_json(record.normalized_text):
            continue  # Schema keys are not research prose; request a source-provenanced narrative projection.
        if {"deleted", "retracted"} & set(record.record_flags):
            continue
        if prepared.policy.discovery_mode == "incremental" and item.window not in {"current", "baseline"}:
            continue
        if remaining <= 0:
            truncated += 1
            continue
        text = record.normalized_text[:remaining]
        if len(text) < len(record.normalized_text):
            truncated += 1
        remaining -= len(text)
        selected.append((item, text))
    return selected, truncated


def phrase_spans(text: str, stoplist: frozenset[str]) -> dict[str, str]:
    phrases = {}
    for segment in SEGMENT.finditer(text):
        words = list(WORD.finditer(segment.group()))
        for size in range(2, 5):
            for start in range(max(0, len(words) - size + 1)):
                chunk = words[start : start + size]
                normalized = [unicodedata.normalize("NFKC", word.group()).lower() for word in chunk]
                if normalized[0] in stoplist or normalized[-1] in stoplist:
                    continue
                if sum(word not in stoplist for word in normalized) < 2:
                    continue
                # Do not join over Chinese text or table separators as English phrases.
                raw = segment.group()[chunk[0].start() : chunk[-1].end()]
                if re.search(r"[\u3400-\u9fff|:]", raw):
                    continue
                phrases.setdefault(" ".join(normalized), raw)
        for run in HAN.finditer(segment.group()):
            for size in range(2, 7):
                for start in range(max(0, len(run.group()) - size + 1)):
                    value = run.group()[start : start + size]
                    if value not in stoplist:
                        phrases.setdefault(value, value)
    return phrases


def _units(items: list[PreparedRecord]) -> set[str]:
    return {item.unit_key for item in items}


def _counts(items: list[PreparedRecord]) -> tuple[int, int]:
    return len(_units([x for x in items if x.window == "current"])), len(
        _units([x for x in items if x.window == "baseline"])
    )


def _stratified_exposure(all_items: list[PreparedRecord], support_items: list[PreparedRecord]) -> dict:
    strata = sorted({(i.record.source_instance, i.record.source_id) for i in all_items})
    rows = []
    weights = []
    for key in strata:
        cohort = [i for i in all_items if (i.record.source_instance, i.record.source_id) == key]
        supporting = [i for i in support_items if (i.record.source_instance, i.record.source_id) == key]
        current_total, baseline_total = _counts(cohort)
        current, baseline = _counts(supporting)
        row = {
            "source_instance": key[0],
            "source_id": key[1],
            **exposure_metrics(current, baseline, current_total, baseline_total),
        }
        rows.append(row)
        if current_total and baseline_total:
            weights.append((baseline_total, current / current_total, baseline / baseline_total))
    denominator = sum(w[0] for w in weights)
    return {
        "source_strata": rows,
        "matched_source_current_rate": sum(w[0] * w[1] for w in weights) / denominator
        if denominator
        else None,
        "matched_source_baseline_rate": sum(w[0] * w[2] for w in weights) / denominator
        if denominator
        else None,
        "source_cohort_stable": bool(rows)
        and all(r["current_denominator"] and r["baseline_denominator"] for r in rows),
    }


def _bins(items: list[PreparedRecord], supporting: set[tuple], prepared: PreparedSnapshot) -> dict:
    policy = prepared.policy
    assert policy.baseline_start and policy.current_end
    count = int((policy.current_end - policy.baseline_start).total_seconds() // (7 * 86400)) + 1
    relevant, exposure = [set() for _ in range(count)], [set() for _ in range(count)]
    for item in items:
        interval = publication_interval(item.record)
        if not interval or item.window not in {"current", "baseline"}:
            continue
        low, high = interval
        index = int((low - policy.baseline_start).total_seconds() // (7 * 86400))
        last = int(
            (
                (high - timedelta(microseconds=1) if high > low else high) - policy.baseline_start
            ).total_seconds()
            // (7 * 86400)
        )
        if 0 <= index < count and index == last:
            exposure[index].add(item.unit_key)
            if item.record.record_ref.version_key in supporting:
                relevant[index].add(item.unit_key)
    value = batch_bursts(
        list(map(len, relevant)),
        list(map(len, exposure)),
        scale=policy.burst_scale,
        penalty=policy.burst_penalty,
        states=policy.burst_states,
    )
    value["bin_days"] = 7
    value["start"] = policy.baseline_start.isoformat()
    value["relevant_counts"] = list(map(len, relevant))
    value["exposure_counts"] = list(map(len, exposure))
    return value


def lexical_emergence(prepared: PreparedSnapshot):
    method = "lexical_emergence"
    texts, truncated = text_items(prepared)
    if not texts:
        return unsupported(method, prepared, "authorized_cleaned_narrative_text_required")
    stop = STOP | frozenset(prepared.policy.stoplist)
    known = {unicodedata.normalize("NFKC", x).lower() for x in prepared.policy.known_vocabulary}
    supports: dict[str, list[PreparedRecord]] = defaultdict(list)
    quotes: dict[str, dict[str, str]] = defaultdict(dict)
    skipped_features = 0
    quality_excluded = defaultdict(int)
    for item, text in texts:
        for phrase, quote in phrase_spans(text, stop).items():
            if phrase in known:
                continue
            if phrase not in supports and len(supports) >= prepared.policy.limits.max_features:
                skipped_features += 1
                continue
            supports[phrase].append(item)
            quotes[phrase][item.record.document_hash] = quote
    all_items = [item for item, _ in texts]
    current_total, baseline_total = _counts(all_items)
    signatures = {phrase: frozenset(_units(items)) for phrase, items in supports.items()}
    # Longest phrase wins only for the same exact supporting unit set.
    ordered = sorted(supports, key=lambda phrase: (-len(phrase), phrase))
    accepted: list[str] = []
    merged_terms = set()
    merged = 0
    for phrase in ordered:
        if phrase in merged_terms:
            merged += 1
            continue
        accepted.append(phrase)
        if HAN.fullmatch(phrase):
            subphrases = {
                phrase[i : i + size] for size in range(2, len(phrase)) for i in range(len(phrase) - size + 1)
            }
        else:
            tokens = phrase.split()
            subphrases = {
                " ".join(tokens[i : i + size])
                for size in range(2, len(tokens))
                for i in range(len(tokens) - size + 1)
            }
        merged_terms.update(sub for sub in subphrases if signatures.get(sub) == signatures[phrase])
    candidates = []
    retained = []
    for phrase in accepted:
        items = supports[phrase]
        current, baseline = _counts(items)
        units = len(signatures[phrase])
        tokens = phrase.split()
        if tokens and tokens[-1] in {"id", "ids", "identifier", "identifiers", "url", "http", "https"}:
            quality_excluded["structural_identifier_suffix"] += 1
            continue
        if len(_units(all_items)) >= 3 and units == len(_units(all_items)):
            quality_excluded["ubiquitous_background_in_adopted_scope"] += 1
            continue
        mode = "stock_exploration"
        if current >= prepared.policy.minimum_support and not baseline:
            mode = "first_observation_in_adopted_current_window"
        elif current >= prepared.policy.minimum_support and baseline_total and current_total:
            metrics = exposure_metrics(current, baseline, current_total, baseline_total)
            if metrics["smoothed_rate_ratio"] >= prepared.policy.minimum_growth:
                mode = "observed_exposure_increase"
        if prepared.policy.discovery_mode == "incremental" and mode == "stock_exploration":
            continue
        if units < prepared.policy.minimum_support:
            continue
        if not current and prepared.policy.discovery_mode == "incremental":
            continue
        metrics = exposure_metrics(current, baseline, current_total, baseline_total)
        metrics.update(_stratified_exposure(all_items, items))
        gaps = sorted({reason for item in items for reason in item.reasons})
        metrics.update(
            {
                "mode": mode,
                "phrase": phrase,
                "deduplicated_document_count": len({i.record.document_hash for i in items}),
                "confirmed_study_count": len({i.unit_key for i in items if i.confirmed_study}),
                "confirmed_source_family_count": len(
                    {i.record.source_family for i in items if i.confirmed_source}
                ),
                "support_unit_count": units,
                "unknown_to_vocabulary": False
                if phrase in known
                else (True if prepared.policy.vocabulary_version else None),
                "growth_confirmable": all(
                    i.growth_eligible for i in all_items if i.window in {"current", "baseline"}
                )
                and current >= 3
                and len({i.record.source_family for i in items if i.confirmed_source}) >= 2
                and metrics["source_cohort_stable"]
                and not truncated
                and not skipped_features
                and not prepared.truncated_count,
            }
        )
        if mode == "stock_exploration":
            metrics["growth_confirmable"] = False
        if truncated or skipped_features or prepared.truncated_count:
            metrics["growth_confirmable"] = False
            gaps.append("processing_scope_incomplete")
        if any(not i.confirmed_study for i in all_items if i.window in {"baseline", "current"}):
            gaps.append("mixed_proxy_denominator")
        score = math.log1p(units) * math.log1p(len(_units(all_items)) / (units + 1))
        score *= min(4, len(tokens) or 1)
        quality_flags = []
        if set(tokens) & {"once", "twice", "daily", "weekly", "week", "day", "month", "year"}:
            quality_flags.append("generic_schedule_or_duration_requires_context")
            score *= 0.05
        metrics["quality_flags"] = quality_flags
        if mode != "stock_exploration":
            score += math.log1p(metrics["smoothed_rate_ratio"])
        retained.append((score, phrase, items, metrics, gaps))
    # Compute temporal paths for only the bounded proposed candidates.
    retained.sort(key=lambda row: (-row[0], row[1]))
    for score, phrase, items, metrics, gaps in retained[: prepared.policy.limits.max_candidates_per_method]:
        if current_total or baseline_total:
            metrics["burst"] = _bins(all_items, {i.record.record_ref.version_key for i in items}, prepared)
        candidates.append(
            candidate(
                method,
                {"phrase": phrase},
                f"文本短语线索：{phrase}",
                items,
                metrics=metrics,
                score=score,
                reasons=(metrics["mode"], "lexical_occurrence_is_not_a_new_mechanism"),
                data_gaps=tuple(gaps),
                quotes=quotes[phrase],
                questions=("该短语指向什么技术或机制，原文条件是什么？", "不同记录是否来自独立研究？"),
                requirements=("S02研究簇与来源独立性", "共享词表版本及专家命名"),
            )
        )
    # Same-segment word pairs are association hypotheses, never extracted causal edges.
    marginals: dict[str, set[str]] = defaultdict(set)
    pairs: dict[tuple[str, str], list[PreparedRecord]] = defaultdict(list)
    pair_quotes: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    pair_overflow = 0
    for item, text in texts:
        for segment in SEGMENT.finditer(text):
            local_text = segment.group()[:400]
            terms = sorted(
                {
                    word.group().lower()
                    for word in WORD.finditer(local_text)
                    if word.group().lower() not in stop and len(word.group()) >= 3
                }
            )[:12]
            for term in terms:
                marginals[term].add(item.unit_key)
            for pair in combinations(terms, 2):
                if pair not in pairs and len(pairs) >= prepared.policy.limits.max_features:
                    pair_overflow += 1
                    continue
                pairs[pair].append(item)
                pair_quotes[pair][item.record.document_hash] = local_text
    exposure = len(_units(all_items))
    association_rows = []
    for pair, items in pairs.items():
        support_count = len(_units(items))
        if support_count < prepared.policy.minimum_support:
            continue
        a, b = len(marginals[pair[0]]), len(marginals[pair[1]])
        cells = [support_count, a - support_count, b - support_count, exposure - a - b + support_count]
        if min(cells) < 0 or not exposure:
            continue
        expected = [
            a * b / exposure,
            a * (exposure - b) / exposure,
            (exposure - a) * b / exposure,
            (exposure - a) * (exposure - b) / exposure,
        ]
        llr = 2 * sum(o * math.log(o / e) for o, e in zip(cells, expected) if o and e)
        association_rows.append((llr, pair, items, support_count))
    association_rows.sort(key=lambda row: (-row[0], row[1]))
    for llr, pair, items, count in association_rows[:10]:
        candidates.append(
            candidate(
                method,
                {"text_pair": pair},
                f"同段文本组合：{pair[0]} × {pair[1]}",
                items,
                kind="association",
                score=llr,
                reasons=("same_segment_cooccurrence", "association_is_not_causation"),
                metrics={
                    "terms": list(pair),
                    "support_units": count,
                    "log_likelihood_ratio": round(llr, 8),
                    "exposure_units": exposure,
                    "statistical_inference": "descriptive_only",
                },
                quotes=pair_quotes[pair],
                questions=("这组共现是否有原文专业关系支持，还是叙述偶然相邻？",),
                requirements=("S02关系断言或S03专业取证",),
            )
        )
    readiness = (
        "ready"
        if prepared.coverage_complete
        and all(x.growth_eligible for x in all_items)
        and not truncated
        and not skipped_features
        else "exploratory_only"
    )
    return result(
        method,
        prepared,
        candidates,
        readiness=readiness,
        reasons=("open_vocabulary_stock_and_incremental_discovery",),
        statistics={
            "text_records": len(texts),
            "phrase_count": len(supports),
            "merged_subphrases": merged,
            "current_denominator": current_total,
            "baseline_denominator": baseline_total,
            "proposed_count_before_limit": len(retained),
            "skipped_feature_observations": skipped_features,
            "text_records_truncated": truncated,
            "record_budget_excluded": prepared.truncated_count,
            "pair_feature_observations_skipped": pair_overflow,
            "quality_excluded_features": dict(quality_excluded),
        },
        truncated=truncated
        + skipped_features
        + pair_overflow
        + max(0, len(retained) - prepared.policy.limits.max_candidates_per_method),
        limitations=(
            "source_mix_changes_can_explain_rate_changes",
            "scores_are_not_confidence_probabilities",
        ),
    )


def _analyzer(text: str) -> list[str]:
    words = [x.group().lower() for x in WORD.finditer(text) if x.group().lower() not in STOP]
    for run in HAN.finditer(text):
        words.extend(run.group()[i : i + 2] for i in range(max(0, len(run.group()) - 1)))
    return words


def text_topics(prepared: PreparedSnapshot):
    method = "text_topics"
    texts, truncated = text_items(prepared)
    if not texts:
        return unsupported(method, prepared, "authorized_cleaned_narrative_text_required")
    # Identical normalized abstracts receive one representation weight. This is
    # text duplication, not an assertion that distinct studies share identity.
    raw_texts = texts
    unique = defaultdict(list)
    for item, text in texts:
        unique[item.record.normalized_text_hash].append((item, text))
    texts = []
    represented_supports = []
    for key in sorted(unique):
        members = sorted(
            unique[key],
            key=lambda row: (
                (publication_interval(row[0].record) or (row[0].record.observed_at,))[0],
                row[0].record.record_ref.version_key,
            ),
        )
        texts.append(members[0])
        represented_supports.append([item for item, _ in members])
    items = [item for item, _ in texts]
    baseline_indices = [i for i, item in enumerate(items) if item.window == "baseline"]
    current_indices = [i for i, item in enumerate(items) if item.window == "current"]
    has_temporal_comparison = bool(baseline_indices and current_indices)
    training = baseline_indices if has_temporal_comparison else list(range(len(texts)))
    mode = "frozen_baseline" if has_temporal_comparison else "stock_exploration"
    vectorizer = TfidfVectorizer(analyzer=_analyzer, max_features=prepared.policy.limits.max_features)
    try:
        train_matrix = vectorizer.fit_transform([texts[i][1] for i in training])
    except ValueError:
        return unsupported(method, prepared, "no_usable_text_features")
    matrix = vectorizer.transform([text for _, text in texts])
    n_components = min(prepared.policy.topic_components, train_matrix.shape[0], train_matrix.shape[1])
    model = NMF(
        n_components=n_components,
        init="nndsvda",
        random_state=prepared.policy.random_seed,
        max_iter=300,
        tol=1e-4,
    )
    with warnings.catch_warnings(record=True) as solver_warnings:
        warnings.simplefilter("always", RuntimeWarning)
        warnings.simplefilter("always", ConvergenceWarning)
        model.fit(train_matrix)
        weights = model.transform(matrix)
    solver_warning_types = sorted({type(warning.message).__name__ for warning in solver_warnings})
    if not np.isfinite(weights).all() or not np.isfinite(model.components_).all():
        raise ValueError("nonfinite NMF factors are not admissible results")
    model_hash = __import__("hashlib").sha256(model.components_.round(8).tobytes()).hexdigest()
    words = vectorizer.get_feature_names_out()
    candidates = []
    assignment = weights.argmax(axis=1)
    for component in range(n_components):
        selected = [i for i, value in enumerate(assignment) if value == component and weights[i].sum() > 0]
        if not selected:
            continue
        top = sorted(range(len(words)), key=lambda i: (-model.components_[component, i], str(words[i])))[:6]
        terms = [str(words[i]) for i in top if model.components_[component, i] > 0]
        if not terms:
            continue
        kind = "association" if len(selected) >= prepared.policy.minimum_support else "hypothesis"
        label = "主题候选" if kind == "association" else "单条文本线索"
        supporting = [item for i in selected for item in represented_supports[i]]
        candidates.append(
            candidate(
                method,
                {"topic": terms, "model": model_hash},
                f"{label}：{' / '.join(terms)}",
                supporting,
                kind=kind,
                score=float(sum(weights[i, component] for i in selected)),
                reasons=(
                    mode,
                    "singletons_are_not_topics" if kind == "hypothesis" else "unsupervised_topic_candidate",
                ),
                metrics={
                    "mode": mode,
                    "terms": terms,
                    "support_units": len(selected),
                    "support_record_count": len(supporting),
                    "model_hash": model_hash,
                    "vocabulary_hash": __import__("hashlib")
                    .sha256("\n".join(map(str, words)).encode())
                    .hexdigest(),
                    "baseline_support": sum(items[i].window == "baseline" for i in selected),
                    "current_support": sum(items[i].window == "current" for i in selected),
                },
                quotes={i.record.document_hash: (i.record.normalized_text or "")[:180] for i in supporting},
                data_gaps=("topic_requires_expert_naming",),
                questions=("这些原文能否构成同一研究主题，是否存在只是模板相同的记录？",),
                requirements=("S03同版本语义表示（可选）", "独立研究簇与人工主题命名"),
            )
        )
    # A vectorizer frozen on baseline cannot discover entirely out-of-vocabulary text.
    oov = []
    if has_temporal_comparison:
        history = matrix[baseline_indices]
        for index in current_indices:
            row = matrix[index]
            distance = 1.0 if not row.nnz else float(1 - cosine_similarity(row, history).max())
            if distance >= prepared.policy.novelty_distance:
                oov.append((index, distance))
        for index, distance in oov:
            item = items[index]
            candidates.append(
                candidate(
                    method,
                    {"novel_record": item.record.record_ref.identity_key},
                    "新颖文本线索，待主题聚合",
                    [item],
                    kind="hypothesis",
                    score=distance,
                    reasons=("historical_lexical_distance", "singletons_are_not_topics"),
                    metrics={"cosine_distance": round(distance, 8), "oov_or_lexically_novel": True},
                    quotes={item.record.document_hash: (item.record.normalized_text or "")[:180]},
                    questions=("该线索是否与其他独立新记录构成共同主题？",),
                    requirements=("K1词表外短语通道",),
                )
            )
        # The current-only model is a proposal representation. It is never used
        # to refit historical topics or claim earlier historical detection.
        novel_indices = [index for index, _ in oov]
        if len(novel_indices) >= prepared.policy.minimum_support:
            cluster_limit = min(len(novel_indices), 500)
            proposed = novel_indices[:cluster_limit]
            try:
                proposal_vectorizer = TfidfVectorizer(
                    analyzer=_analyzer, max_features=prepared.policy.limits.max_features
                )
                proposal_matrix = proposal_vectorizer.fit_transform([texts[i][1] for i in proposed])
                labels = DBSCAN(
                    eps=max(1e-6, 1 - prepared.policy.topic_similarity),
                    min_samples=max(2, prepared.policy.minimum_support),
                    metric="cosine",
                )
                assignments = labels.fit_predict(proposal_matrix)
            except ValueError:
                assignments = np.full(len(proposed), -1)
            for cluster in sorted(set(assignments) - {-1}):
                indices = [proposed[i] for i, assigned in enumerate(assignments) if assigned == cluster]
                supporting = [item for i in indices for item in represented_supports[i]]
                terms = sorted({word for i in indices for word in _analyzer(texts[i][1])})[:8]
                candidates.append(
                    candidate(
                        method,
                        {"new_cluster": sorted(items[i].record.normalized_text_hash for i in indices)},
                        "当前多文本新颖主题候选",
                        supporting,
                        kind="hypothesis",
                        score=len(indices),
                        reasons=("current_only_novel_cluster_requires_expert_naming",),
                        metrics={
                            "terms": terms,
                            "unique_text_count": len(indices),
                            "representation_scope": "current_novel_proposal_only",
                            "model_hash": model_hash,
                        },
                        quotes={
                            item.record.document_hash: (item.record.normalized_text or "")[:180]
                            for item in supporting
                        },
                        questions=("共同主题是否有独立科学内容支持，是否只是改写/模板？",),
                        requirements=("独立研究簇与S03同版本表示", "专家主题命名"),
                    )
                )
            truncated += len(novel_indices) - cluster_limit
    embedding_items = [
        (i, item.record.embedding)
        for i, item in enumerate(items)
        if item.record.embedding
        and section_visible(item.record.embedding.provenance, item.record, prepared.policy)
    ]
    signatures = {
        (item.record.embedding.provider, item.record.embedding.model_version, item.record.embedding.dimension)
        for item, _ in raw_texts
        if item.record.embedding
        and section_visible(item.record.embedding.provenance, item.record, prepared.policy)
    }
    embedding_status = "unsupported"
    if len(signatures) == 1:
        history_vectors = [(i, e) for i, e in embedding_items if items[i].window == "baseline"]
        new_vectors = [(i, e) for i, e in embedding_items if items[i].window == "current"]
        if history_vectors and new_vectors:
            embedding_status = "success"
            historical_matrix = np.array([e.vector for _, e in history_vectors])
            for index, embedding in new_vectors:
                similarities = cosine_similarity(np.array([embedding.vector]), historical_matrix)[0]
                nearest = int(similarities.argmax())
                distance = float(1 - similarities[nearest])
                if distance >= prepared.policy.novelty_distance:
                    item = items[index]
                    candidates.append(
                        candidate(
                            method,
                            {
                                "embedding_record": item.record.record_ref.identity_key,
                                "model": embedding.model_version,
                            },
                            "语义新颖文本线索",
                            [item],
                            kind="hypothesis",
                            score=distance,
                            reasons=("same_model_embedding_distance", "singletons_are_not_topics"),
                            metrics={
                                "cosine_distance": round(distance, 8),
                                "model_version": embedding.model_version,
                                "nearest_history_ref": items[
                                    history_vectors[nearest][0]
                                ].record.record_ref.model_dump(mode="json"),
                            },
                            quotes={item.record.document_hash: (item.record.normalized_text or "")[:180]},
                            questions=("该语义差异是否为内容新颖，还是语言/格式/模板差异？",),
                            requirements=("独立来源与同模型当前多文档支持",),
                        )
                    )
    return result(
        method,
        prepared,
        candidates,
        statistics={
            "text_units": len(items),
            "raw_text_records": len(raw_texts),
            "normalized_text_duplicates_merged": len(raw_texts) - len(items),
            "components": n_components,
            "model_scope": mode,
            "model_hash": model_hash,
            "embedding_variant_status": embedding_status,
            "embedding_model_conflict": len(signatures) > 1,
            "oov_novel_text_count": len(oov),
            "nmf_solver_warning_types": solver_warning_types,
            "nmf_iterations": int(model.n_iter_),
            "novel_cluster_proposal_record_limit": 500,
        },
        partial=bool(solver_warning_types),
        truncated=truncated,
        limitations=("topic_or_distance_is_not_scientific_novelty", "no_significance_claim"),
    )
