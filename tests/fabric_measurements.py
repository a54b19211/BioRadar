"""A stand-in for Fabric's measurement routes - the change feed (datafabric.measurement_changes.v1) and the
pull by version (datafabric.measurements.v1) - in the shapes of Fabric's own contract fixture
(tests/fixtures/measurement_feed_contract.json, a byte copy of clean-fabric's, pinned in test_series_sync)
and of its receipt (db/measurement_reads.receipt: a MeasurementV2 body plus the state at the cutoff).

Objects are added, corrected, retracted or superseded as events, each with a feed sequence number. An event of
a revoked source - or whose cause comes from one - is withheld from the feed (counted per source), a pull of a
revoked source answers 403 source_not_allowed, and a correction whose cause source is revoked is not applied
to the state a pull returns: Fabric's fold."""

import hashlib

from bioradar.adapters.fabric import FabricError

KINDS = ("created", "superseded_by_processor", "corrected", "retracted", "correction_withdrawn")
CHECKER = "biocheck.extraction_rules.v17"


def hexid(*parts):
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def version_id(name):
    return hexid("version", name)


class Measured:
    def __init__(self):
        self.objects, self.events, self.revoked, self.calls = {}, [], set(), []

    # ---- what happens in Fabric ------------------------------------------------------------------------

    def _event(self, mid, kind, *, cause_source=None, cause=None, by=None, at="2026-10-29T08:00:00+00:00"):
        obj = self.objects[mid]
        e = {
            "measurement_id": mid,
            "version_id": obj["inputs"][0]["version_id"],
            "source_id": obj["inputs"][0]["source_id"],
            "kind": kind,
            "seq": len(self.events) + 1,
            "produced_at": at,
            "effective_at": at,
            "material_visible_at": at,
            "versions": self._versions(),
            "cause_source": cause_source,
        }
        if cause is not None:
            e["cause"] = cause
        if by is not None:
            e["superseded_by"] = by
        self.events.append(e)
        return e

    def add(
        self,
        version,
        *,
        source="who_don",
        record="2026-DON512",
        value="66",
        metric="报告马尔堡病毒病确诊病例",
        obj="卢旺达卫生部",
        unit="例",
        scope="在卢旺达全国",
        period=None,
        kind="cumulative",
        family="case_count",
        dims=None,
        unlocated=(),
        identity=None,
        check="consistent",
        seen="2026-10-29T00:00:00+00:00",
        quote=None,
    ):
        """One checked object of a fixed version (its created event is in the feed). Returns its id."""
        mid = hexid("measurement", source, record, version, value, metric, obj, period, len(self.objects))
        quote = quote or f"截至2026年10月28日，{obj}{scope}累计共{metric}{value}{unit}"
        dims = [{"kind": "disease", "value": "马尔堡病毒病", "check": "match"}] if dims is None else dims
        self.objects[mid] = {
            "measurement_id": mid,
            "reading_id": hexid("reading", mid),
            "contract_version": "datafabric.data_objects.v2",
            "kind": "measurement",
            "inputs": [{"source_id": source, "record_id": record, "version_id": version, "role": "primary"}],
            "processing": {
                "processor_version": "datafabric.measure_extract.v1",
                "config_sha256": hexid("cfg"),
            },
            "artifact": {
                "text_processor_version": "datafabric.clean_text.v12",
                "text_sha256": hexid("text", version),
            },
            "locators": [
                {
                    "kind": "char_span",
                    "version_id": version,
                    "start": 10,
                    "end": 10 + len(quote),
                    "derived_text_sha256": hexid("text", version),
                }
            ],
            "permission_sources": [source],
            "visibility": {"first_seen_at": seen, "proof_available_at": None, "proof_refs": []},
            "object": obj,
            "metric": metric,
            "value_text": value,
            "value_literal": value,
            "unit": unit,
            "period": period or {"type": "point", "precision": "day", "value": "2026-10-28"},
            "period_literal": "截至2026年10月28日",
            "scope": scope or None,
            "scope_mode": "stated" if scope else "unknown",
            "numerator_text": None,
            "denominator_text": None,
            "denominator_state": "not_applicable" if kind != "rate" else "unknown",
            "measure_kind": kind,
            "qualifier": "exact",
            "modality": "actual",
            "family": family,
            "dims": [dict({"start": 0, "end": len(d["value"])}, **d) for d in dims],
            "dims_unlocated": list(unlocated),
            "check": {
                "checker_version": CHECKER,
                "contract_version": "extraction_consistency.v3",
                "run_id": "r-" + mid[:6],
                "status": check,
            },
            "quote": quote,
            "identity": identity
            or {"state": "provisional", "key": f"literal:{source}|{record}|{obj.casefold()}", "graph": None},
        }
        self._event(mid, "created")
        return mid

    def correct(self, mid, *, relation="corrects", by_source="who_don", by_record="2026-DON513"):
        cause = {
            "source_id": by_source,
            "record_id": by_record,
            "version_id": version_id(by_record),
            "relation": relation,
        }
        kind = "retracted" if relation == "retracts" else "corrected"
        return self._event(mid, kind, cause_source=by_source, cause=cause)

    def withdraw_correction(self, mid, *, by_source="who_don"):
        return self._event(
            mid, "correction_withdrawn", cause_source=by_source, cause={"source_id": by_source}
        )

    def supersede(self, mid, by):
        return self._event(mid, "superseded_by_processor", by=by)

    # ---- what the radar reads ----------------------------------------------------------------------------

    def _versions(self):
        return {
            "data_objects": "datafabric.data_objects.v2",
            "producer": "datafabric.measure_extract.v1:" + hexid("cfg")[:12],
            "checker": CHECKER,
            "contract": "extraction_consistency.v3",
            "text_processor": "datafabric.clean_text.v12",
        }

    def _blocked(self, e):
        return next((s for s in (e["source_id"], e["cause_source"]) if s and s in self.revoked), None)

    def changes(self, *, is_demo, after_seq=0, limit=50):
        self.calls.append(("changes", after_seq, limit))
        rows = [e for e in self.events if e["seq"] > after_seq][: limit + 1]
        page, items, withheld = rows[:limit], [], {}
        for e in page:
            blocked = self._blocked(e)
            if blocked:
                withheld[blocked] = withheld.get(blocked, 0) + 1
                continue
            items.append({k: v for k, v in e.items() if k != "cause_source"})
        return {
            "schema": "datafabric.measurement_changes.v1",
            "is_demo": is_demo,
            "after_seq": after_seq,
            "next_after": page[-1]["seq"] if page else after_seq,
            "more": len(rows) > limit,
            "items": items,
            "withheld": withheld,
        }

    def _state(self, mid):
        status, applied, active = {"state": "ok"}, [], {}
        for e in (e for e in self.events if e["measurement_id"] == mid):
            if e["kind"] != "created" and e["cause_source"] in self.revoked:
                continue  # its cause cannot be read now: not applied
            summary = {
                "kind": e["kind"],
                "seq": e["seq"],
                "produced_at": e["produced_at"],
                "effective_at": e["effective_at"],
                "material_visible_at": e["material_visible_at"],
            }
            if "cause" in e:
                summary["cause"] = e["cause"]
            applied.append(summary)
            if e["kind"] == "superseded_by_processor":
                status = {"state": "superseded", "reason": e["kind"], "superseded_by": e["superseded_by"]}
            elif e["kind"] == "correction_withdrawn":
                active.clear()
            elif e["kind"] in ("corrected", "retracted"):
                active[e["cause"]["record_id"]] = e["kind"]
        if status["state"] != "superseded" and active:
            retracted = "retracted" in active.values()
            status = {
                "state": "retracted" if retracted else "corrected",
                "reason": "retracted_by_source" if retracted else "corrected_by_source",
            }
        return status, applied

    def receipt(self, mid, mode="current", cutoff=None):
        body = self.objects[mid]
        status, applied = self._state(mid)
        current = status["state"] == "ok"
        return dict(
            body,
            status=status,
            mode=mode,
            cutoff=cutoff,
            state_at_cutoff=status["state"],
            events_applied=applied,
            events_after_cutoff=0,
            events_withheld=0,
            reconstruction=False,
            current=current,
            not_current_reason=None if current else status["state"],
            unresolved_corrections=[],
            versions=self._versions(),
        )

    def measurements(self, source, version_ids, *, is_demo, mode, cutoff, include_history=False):
        self.calls.append(("measurements", source, tuple(version_ids)))
        if source in self.revoked:
            raise FabricError("source_not_allowed", "当前许可不允许该来源")
        items = [
            self.receipt(m, mode, cutoff)
            for m, o in self.objects.items()
            if o["inputs"][0]["source_id"] == source and o["inputs"][0]["version_id"] in version_ids
        ]
        return {
            "schema": "datafabric.measurements.v1",
            "source_id": source,
            "is_demo": is_demo,
            "mode": mode,
            "cutoff": cutoff,
            "measurements": [r for r in items if r["current"] or include_history],
            "excluded": {},
        }
