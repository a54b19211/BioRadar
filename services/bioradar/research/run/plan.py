"""Stage plan: the model's plan turned into checks, searches and hypotheses the program numbers and
scopes; the question's objects looked up in the reviewed graph; a fixed material set's records as
reading candidates."""

import json
import re
from typing import TYPE_CHECKING

import httpx

from bioradar.adapters.fabric import FabricError
from bioradar.research import prompts, schemas
from bioradar.research.lineage import previous_check
from bioradar.research.run import Failed, search
from bioradar.research.run.search import FABRIC_PAGE, clean_terms
from bioradar.text import squash

if TYPE_CHECKING:
    from bioradar.research.pipeline import Run


CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*$")


def code_roots(terms):
    """Drug and trial codes refer to each other by a shared letter root: XQ-17, DEMO-XQ301 -> XQ.

    Fabric matches terms literally, so a code-like term also gets its letter roots as single-term queries."""
    roots = []
    for term in terms:
        if CODE.match(term):
            for part in re.findall(r"[A-Za-z]{2,}", term):
                if part.upper() not in {"DEMO", "TEST", "NCT", "ID"} and [part] not in roots:
                    roots.append([part])
    return roots


def plan(run: "Run"):
    """The model's plan as the run's checks, searches, hypotheses and judgments; the program numbers them,
    decides where every planned name comes from, and adds the seeds, graph records and a small fixed
    material set as the first reading candidates."""
    model, shown = ask_plan(run)
    planned = [o.name.strip() for o in model.objects[:8]]

    def basis(name):
        return name_basis(name, shown)

    named = [n if n and basis(n) == "question" else "" for n in planned]  # check indexes
    objects = list(dict.fromkeys(n for n in named if n))
    checks = plan_checks(run, model, named)
    required = [c["id"] for c in checks if c["required"]]
    queries = plan_queries(model, checks, required)
    terms = [t for q in queries for t in q["terms"]]
    plan_basis, plan_removed = name_bases(model, planned, terms, basis)
    mark_exploratory(run, checks, [n for n, b in plan_basis.items() if b != "question"])
    run.p.update(
        hypotheses=plan_hypotheses(model),
        objects=objects,
        plan_basis=plan_basis,
        plan_removed=plan_removed,
        checks=checks,
        queries=queries,
        judgments=model.judgments[:4],
        needs_calculation=model.needs_calculation or run.input["direction"] == "business",
        mechanism=model.mechanism.model_dump() if model.mechanism else None,
        actions=[],
        hits=[],
        facts=[],
        relations=[],
        pages={},
        refs={},
        retries=0,
    )
    first_candidates(run, required)


def ask_plan(run: "Run"):
    """(the model's plan, (question, earlier report) as squashed text): the earlier round's report goes in
    only while its sources can be read, and whether it did is kept (used_previous)."""
    payload = {
        "question": run.asked,
        "direction": run.input["direction"],
        "history_cutoff": run.input.get("cutoff"),
    }
    previous = run.previous_report()
    run.p["used_previous"] = bool(previous)
    if previous:
        payload["previous_report"] = previous["draft"][:3000]
        payload["previous_report_check"] = previous_check(previous)
    model = run.ask("plan", prompts.PLAN, payload, schemas.Plan, uses_previous=bool(previous))
    if model is None or not model.checks:
        raise Failed("plan_failed")
    return model, (squash(run.input["question"]), squash(payload.get("previous_report", "")))


def name_basis(name, shown):
    """Where a planned name comes from, decided here and not reported by the model: the question, the
    previous report (a draft, not an original record), or nowhere (exploratory). Only names from the
    question become objects; the others may steer searching but never narrow what counts as relevant.
    plan_basis records every planned name's basis and plan_removed the objects left out with the checks
    they were attached to (the model's raw output is not kept in progress)."""
    name = squash(name)
    return "question" if name in shown[0] else "prior_report" if name in shown[1] else "exploratory"


def name_bases(model, planned, terms, basis):
    """(plan_basis: every planned name and search term with its basis, plan_removed: the planned objects
    not from the question, with the checks they were attached to)."""
    plan_basis = {n: basis(n) for n in dict.fromkeys([n for n in planned if n] + terms)}
    plan_removed = [
        {
            "name": n,
            "basis": basis(n),
            "checks": [f"Q{i}" for i, c in enumerate(model.checks[:8], 1) if k in c.objects],
        }
        for k, n in enumerate(planned, 1)
        if n and basis(n) != "question"
    ]
    return plan_basis, plan_removed


def plan_hypotheses(model):
    return [
        {"id": f"H{n}", **h.model_dump()}
        for n, h in enumerate(model.hypotheses[:3], 1)
        if h.statement.strip()
    ]


def plan_checks(run: "Run", model, named):
    """The checks (Q1 ...), each with the question's objects it is about; at least one is required."""
    checks = [
        {
            "id": f"Q{i}",
            "text": c.text,
            "required": c.required,
            "importance": 1.0 if c.required else 0.5,
            "status": "open",
            "evidence": [],
            # names of the objects this check is about (R8a: answered only when each is covered)
            "objects": list(
                dict.fromkeys(named[n - 1] for n in c.objects if 1 <= n <= len(named) and named[n - 1])
            ),
        }
        for i, c in enumerate(model.checks[:8], 1)
    ]
    for c, planned_check in zip(checks, model.checks):
        # a check only about objects the question never named is a direction, never required
        if planned_check.objects and not c["objects"]:
            c["required"], c["importance"] = False, 0.5
    if not any(c["required"] for c in checks):
        anchor = next((c for c, pc in zip(checks, model.checks) if not pc.objects or c["objects"]), None)
        if anchor is None:  # every check is about names the question never gave: ask the question itself
            anchor = {
                "id": f"Q{len(checks) + 1}",
                "text": run.input["question"][:300],
                "importance": 1.0,
                "status": "open",
                "evidence": [],
                "objects": [],
            }
            checks.append(anchor)
        anchor["required"] = True
    return checks


def plan_queries(model, checks, required):
    """The planned searches, cleaned and deduplicated, and each code-like term's letter roots."""
    queries = []
    for q in model.queries[:10]:
        terms = clean_terms(q.terms)
        if terms and all(terms != x["terms"] for x in queries):
            ids = [f"Q{n}" for n in q.checks if 1 <= n <= len(checks)]
            queries.append({"terms": terms, "checks": ids or required, "origin": "plan"})
    for root in code_roots([t for q in queries for t in q["terms"]]):
        if all(root != q["terms"] for q in queries):
            queries.append({"terms": root, "checks": required, "origin": "plan"})
    if not queries:
        raise Failed("plan_without_queries")
    return queries


def mark_exploratory(run: "Run", checks, exploratory):
    for c in checks:
        # shown to the reader as search directions that do not limit the question's scope
        c["exploratory"] = [n for n in exploratory if squash(n) in squash(c["text"])]
    if any(c["exploratory"] for c in checks):
        run.note(
            "plan_exploratory_scope",
            "；".join(f"{c['id']}:{'、'.join(c['exploratory'])}" for c in checks),
        )


def first_candidates(run: "Run", required):
    """The seed records, the question's objects' graph records, and a small fixed material set's records
    visible at the cutoff, as reading candidates."""
    for seed in run.input.get("seed_records", []):
        # a correction notice is pinned to the version that corrected; other seeds are not
        search.add_hit(
            run,
            seed["source"],
            seed["record_id"],
            seed.get("title", ""),
            required,
            critical=True,
            version_id=seed.get("version_id"),
        )
    graph(run, required)
    # A fixed material set: searches stay inside it, and a small set's records visible at the
    # cutoff are reading candidates.
    if run.scope and len(run.scope) <= FABRIC_PAGE:
        for source in run.input["sources"]:
            docs, _ = run.fabric.search(
                source,
                (),
                is_demo=run.input["is_demo"],
                cutoff=run.cutoff,
                record_ids=run.scope,
                limit=len(run.scope),
            )
            for doc in docs:
                run.docs[(doc["source"], doc["record_id"])] = doc
                search.add_hit(run, doc["source"], doc["record_id"], doc["title"], required, critical=True)


def graph(run: "Run", checks):
    """The question's own objects looked up in Tang's reviewed graph (read only, through Fabric).
    Graph records visible at the cutoff that mention the exact name are resolved; each candidate keeps
    its identity status (unresolved stays unresolved) and the records it was read from become reading
    candidates, re-read from Fabric at the cutoff like any other hit. Nothing is merged or inferred."""
    if not run.s.graph_lookup:
        return
    found, seen = run.p.setdefault("graph", []), set()
    for name in run.p["objects"][:3]:
        entry = {"object": name, "graph_records": [], "candidates": [], "refs": []}
        try:
            records, _ = run.fabric.search(
                "reviewed_graph", [name], is_demo=run.input["is_demo"], cutoff=run.cutoff, limit=5
            )
            resolutions = [(r, run.fabric.graph_resolve(r, name, cutoff=run.cutoff)) for r in records]
        except (FabricError, httpx.HTTPError) as exc:
            run.note("graph_unavailable", f"{name}: {getattr(exc, 'code', type(exc).__name__)}")
            continue
        for record, resolution in resolutions:
            entry["graph_records"].append(record["record_id"])
            try:  # a graph record is one bundle; its document_refs are the documents it was built from
                bundle_refs = json.loads(record["text"] or "{}").get("document_refs") or []
            except ValueError:
                bundle_refs = []
            for node in resolution.get("candidates") or []:
                entry["candidates"].append(
                    {
                        "id": node.get("id"),
                        "name": node.get("name"),
                        "identity": node.get("identity_status"),
                    }
                )
                # the node's own evidence, else the documents of the bundle it was found in
                refs = [(ev, "node") for ev in node.get("evidence") or []] or [
                    (ev, "bundle") for ev in bundle_refs
                ]
                for ev, basis in refs:
                    key = (ev["source_id"], ev["source_record_id"])
                    if ev["source_id"] not in run.input["sources"] or key in seen:
                        continue
                    seen.add(key)
                    entry["refs"].append(
                        {
                            "source": key[0],
                            "record_id": key[1],
                            "version_id": ev["version_id"],
                            "basis": basis,
                        }
                    )
                    search.add_hit(run, *key, f"图谱：{node.get('name')}", checks)
        found.append(entry)
