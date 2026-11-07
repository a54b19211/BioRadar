"""Fabric's source list (GET /api/history/sources) as the tests' fake Fabrics answer it.

tests/fixtures/fabric_sources_receipt.json is a receipt produced by Fabric's own listing code
(db.source_listing on the r2/fabric-b1 branch) for every catalogued source, readable; the helpers below derive
the receipt of a given set of readable sources from it the way Fabric does: a source that is not readable is
listed under unavailable_sources with every capability false, a retired source under retired_sources."""

import copy
import json
from pathlib import Path

from bioradar.adapters.capabilities import SourceCapabilities

RECEIPT = json.loads((Path(__file__).parent / "fixtures" / "fabric_sources_receipt.json").read_text("utf-8"))
_ENTRIES = {e["source_id"]: e for e in RECEIPT["sources"] + RECEIPT["retired_sources"]}
_PLAIN = _ENTRIES["external_archive"]
_OFF = {
    "readable": False,
    "read": False,
    "list": False,
    "search": False,
    "select": False,
    "fetch": None,
    "graph": False,
    "watch": False,
    "attachments": None,
    "clean_text": None,
    "registry_diff": None,
    "material_kind": None,
}


def entry(source, **capabilities):
    """The receipt entry of one source: the catalogued one, or for a name the catalog does not know a plain
    original; `capabilities` override fields of its capabilities object."""
    found = copy.deepcopy(_ENTRIES.get(source) or dict(_PLAIN, source_id=source))
    found["source_id"] = source
    found["capabilities"].update(capabilities)
    return found


def sources_receipt(readable, *, known=()):
    """Fabric's receipt when exactly `readable` source ids can be read now. `known`: other ids Fabric declares
    but that are not readable (unavailable_sources)."""
    readable = set(readable)
    out = {k: v for k, v in RECEIPT.items() if k not in ("sources", "retired_sources", "unavailable_sources")}
    out["sources"], out["retired_sources"], out["unavailable_sources"] = [], [], []
    for source in sorted(readable):
        item = entry(source)
        retired = item["capabilities"].get("retired")
        (out["retired_sources"] if retired else out["sources"]).append(item)
    for source in sorted(set(known) - readable):
        out["unavailable_sources"].append({"source_id": source, "capabilities": dict(_OFF)})
    return out


def capabilities_of(*readable):
    """The parsed capabilities of a Fabric where exactly these sources are readable."""
    return SourceCapabilities.parse(sources_receipt(readable))
