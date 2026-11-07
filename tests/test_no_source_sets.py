"""Fabric is the one authority on what each source can do. The radar keeps no list of sources of its own: no
set of fetchable, attachment or registry-diff sources, no reading of a source's own metadata to name the
kind of material. This scan fails when one comes back, whatever it is called."""

import ast
from pathlib import Path

from fabric_receipts import RECEIPT

PACKAGE = Path(__file__).resolve().parents[1] / "services" / "bioradar"
# every source Fabric's catalog declares (the committed real receipt), the retired one included
SOURCE_IDS = {e["source_id"] for e in RECEIPT["sources"] + RECEIPT["retired_sources"]}
# the names the copies had; they may not be defined again (as a variable, a function or a class)
FORBIDDEN_NAMES = {"FETCHABLE", "ATTACHMENT_SOURCES", "REGISTRY_DIFF_SOURCES", "native_kind"}
# the operator's configuration is where a deployment names the sources it uses; nothing else may
CONFIGURATION = {"config.py"}


def literal_sources(node) -> set[str]:
    """Source ids named by string elements of a collection literal (set, frozenset(...), list, tuple, dict
    keys or values) found anywhere in the node."""
    found = set()
    for inner in ast.walk(node):
        if isinstance(inner, (ast.Set, ast.List, ast.Tuple)):
            found |= {e.value for e in inner.elts if isinstance(e, ast.Constant) and e.value in SOURCE_IDS}
        elif isinstance(inner, ast.Dict):
            parts = [*inner.keys, *inner.values]
            found |= {e.value for e in parts if isinstance(e, ast.Constant) and e.value in SOURCE_IDS}
    return found


def offences(tree: ast.AST) -> list[str]:
    """What in one module is a copy of Fabric's knowledge: a forbidden name, or a module-level value that is a
    collection holding two or more Fabric source ids."""
    found = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
        found += [f"{name} (line {node.lineno})" for name in names if name in FORBIDDEN_NAMES]
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            ids = literal_sources(node.value)
            if len(ids) >= 2:
                found.append(f"a collection of sources {sorted(ids)} (line {node.lineno})")
    return found


def modules():
    return sorted(p for p in PACKAGE.rglob("*.py") if p.name not in CONFIGURATION)


def test_the_radar_holds_no_list_of_fabric_sources():
    assert modules(), "the package was not found"
    bad = {p.relative_to(PACKAGE).as_posix(): offences(ast.parse(p.read_text("utf-8"))) for p in modules()}
    assert {k: v for k, v in bad.items() if v} == {}


def test_the_scan_catches_each_way_a_copy_comes_back():
    cases = {
        "FETCHABLE = {'pubmed', 'europe_pmc'}": 2,
        "SOURCES_WITH_FILES = frozenset({'nmpa', 'cde', 'chictr'})": 1,
        "READ = ('hkex', 'fda_press')": 1,
        "BY = {'pubmed': 1, 'nmpa': 2}": 1,
        "ATTACHMENT_SOURCES: set = set()": 1,
        "def native_kind(record):\n    return None": 1,
        "class REGISTRY_DIFF_SOURCES: pass": 1,
    }
    for source, count in cases.items():
        assert len(offences(ast.parse(source))) == count, source
    # one source id in a default, or ids outside Fabric's catalog, are not a list of Fabric's sources
    assert offences(ast.parse("DEFAULT = ['pubmed']\nOTHER = {'x', 'y'}")) == []
