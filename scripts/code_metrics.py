"""Function size and cyclomatic complexity of the Python sources, with a CI guard.

    python scripts/code_metrics.py [paths...]          # report: file sizes, the longest, the most complex
    python scripts/code_metrics.py --check [paths...]  # fail on a function over the limits without a reason

A function's length is its lines from `def` to its last line. Its complexity is McCabe's: 1 plus one per
if / elif / for / while / except / with-less branch point, conditional expression, comprehension `for` and
`if`, `case`, and each extra operand of `and` / `or`. Nested functions and lambdas count towards the function
that holds them (moving a branch into a closure does not make the function simpler) and are also listed on
their own.

The limits are 80 lines and complexity 20. A function may exceed them only with a written reason in
scripts/metrics_exceptions.txt (`path:qualified.name  reason`); --check also fails on an entry that is no
longer needed, so the list only shrinks."""

import argparse
import ast
import pathlib
import sys

MAX_LINES, MAX_CC = 80, 20
ROOT = pathlib.Path(__file__).resolve().parent.parent
EXCEPTIONS = ROOT / "scripts" / "metrics_exceptions.txt"
DEFAULT_PATHS = ("services",)


def complexity(node):
    cc = 1
    for n in ast.walk(node):
        if isinstance(n, ast.If | ast.For | ast.AsyncFor | ast.While | ast.IfExp | ast.ExceptHandler):
            cc += 1
        elif isinstance(n, ast.BoolOp):
            cc += len(n.values) - 1
        elif isinstance(n, ast.comprehension):
            cc += 1 + len(n.ifs)
        elif isinstance(n, ast.match_case):
            cc += 1
    return cc


def functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{child.name}"
                yield name, child.lineno, child.end_lineno - child.lineno + 1, complexity(child)
                yield from walk(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.")
            else:
                yield from walk(child, prefix)

    yield from walk(tree, "")


def collect(paths):
    files, rows = {}, []
    for base in paths:
        base = pathlib.Path(base)
        for path in sorted(base.rglob("*.py") if base.is_dir() else [base]):
            rel = path.resolve().relative_to(ROOT).as_posix()
            files[rel] = len(path.read_text(encoding="utf-8").splitlines())
            rows += [(rel, name, line, size, cc) for name, line, size, cc in functions(path)]
    return files, rows


def exceptions():
    out = {}
    if EXCEPTIONS.exists():
        for line in EXCEPTIONS.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                key, _, reason = line.partition("  ")
                out[key.strip()] = reason.strip()
    return out


def report(files, rows, top):
    print("largest files:")
    for rel, n in sorted(files.items(), key=lambda x: -x[1])[:top]:
        print(f"  {n:6d}  {rel}")
    print("longest functions:")
    for rel, name, line, size, cc in sorted(rows, key=lambda r: -r[3])[:top]:
        print(f"  {size:6d} lines  cc {cc:3d}  {rel}:{line} {name}")
    print("most complex functions:")
    for rel, name, line, size, cc in sorted(rows, key=lambda r: -r[4])[:top]:
        print(f"  cc {cc:3d}  {size:6d} lines  {rel}:{line} {name}")
    over = [r for r in rows if r[3] > MAX_LINES or r[4] > MAX_CC]
    print(f"{len(rows)} functions; {len(over)} over {MAX_LINES} lines or complexity {MAX_CC}")


def check(rows):
    allowed, problems = exceptions(), []
    over = {f"{rel}:{name}": (size, cc) for rel, name, _, size, cc in rows if size > MAX_LINES or cc > MAX_CC}
    for key, (size, cc) in sorted(over.items()):
        if key not in allowed:
            problems.append(
                f"{key}: {size} lines, complexity {cc} (limits {MAX_LINES} / {MAX_CC}), no reason"
            )
    for key in sorted(set(allowed) - set(over)):
        problems.append(f"{key}: listed in {EXCEPTIONS.name} but within the limits now - remove the entry")
    for p in problems:
        print(p)
    return 1 if problems else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", default=[ROOT / p for p in DEFAULT_PATHS])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    files, rows = collect(args.paths)
    if args.check:
        sys.exit(check(rows))
    report(files, rows, args.top)


if __name__ == "__main__":
    main()
