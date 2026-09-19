"""Read a git repo into a plain dict: files, symbols, imports, tests, manifests."""

import ast
import re
import subprocess
from pathlib import Path

CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}
MANIFESTS = {
    "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
    "package.json", "go.mod", "Cargo.toml",
}
# Files that two branches conflict in constantly and for boring reasons:
# lockfiles, CI config, changelogs. Empirically the highest-conflict category
# in a real repo, and the one where "merge carefully" is the wrong advice.
LOCK_NAMES = {
    "poetry.lock", "uv.lock", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "pipfile.lock", "cargo.lock", "composer.lock",
}

# ponytail: regex for TS/JS instead of tree-sitter. Swap in tree-sitter when
# the false positives start mattering.
TS_SYMBOL = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?"
    r"(?:function\s+(\w+)|class\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s*)?\()",
    re.M,
)
TS_IMPORT = re.compile(r"""(?:from|require\()\s*['"]([^'"]+)['"]""")


def git(repo, *args):
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def scan(repo, rev=None):
    """Read the working tree, or any commit if you pass rev.

    Reading an old commit is what makes backfill possible: you cannot grade a
    forecast fairly against a repo that already contains the answer.
    """
    repo = Path(repo).resolve()
    sha = git(repo, "rev-parse", rev or "HEAD")
    tracked = [
        p for p in (
            git(repo, "ls-tree", "-r", "--name-only", rev) if rev
            else git(repo, "ls-files")
        ).splitlines() if p
    ]

    files = {}
    for rel in tracked:
        path = repo / rel
        if path.suffix not in CODE_SUFFIXES:
            continue
        if rev:
            source = read_at(repo, rev, rel)
            if source is None:
                continue
        else:
            if not path.is_file():
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        symbols, imports = (
            _parse_python(source) if path.suffix == ".py" else _parse_ts(source)
        )
        files[rel] = {
            "symbols": symbols,
            "imports": imports,
            "is_test": _is_test(rel),
            "lines": source.count("\n") + 1,
        }

    return {
        "repo": str(repo),
        "sha": sha,
        "rev": rev,
        "branch": rev or git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "files": files,
        "manifests": [p for p in tracked if Path(p).name in MANIFESTS],
        "schema_files": [p for p in tracked if _is_schema(p)],
        "regenerated_files": [p for p in tracked if is_regenerated(p)],
        "callers": _callers(files),
    }


def read_at(repo, rev, path):
    """Contents of one file at one commit, or None if it was not there."""
    out = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True, text=True,
    )
    return out.stdout if out.returncode == 0 else None


def _is_test(rel):
    name = Path(rel).name
    return (
        name.startswith("test_")
        or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts"))
        or "tests/" in rel
    )


def is_regenerated(rel):
    """Lockfiles, CI workflows and changelogs: rebuilt or appended to, not merged."""
    low = rel.lower()
    name = Path(low).name
    return (
        name in LOCK_NAMES
        or name.endswith(".lock")
        or ".github/workflows/" in low
        or name.startswith(("changes", "changelog", "history"))
    )


def _is_schema(rel):
    low = rel.lower()
    return (
        "migration" in low
        or "/schema" in low
        or Path(low).name in {"schema.py", "schema.sql", "models.py", "schema.prisma"}
        or low.endswith(".sql")
    )


def _parse_python(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], []
    symbols, imports = [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({
                "name": node.name,
                "kind": "function",
                "line": node.lineno,
                "signature": _signature(node),
                "doc": (ast.get_docstring(node) or "")[:200],
            })
        elif isinstance(node, ast.ClassDef):
            symbols.append({
                "name": node.name,
                "kind": "class",
                "line": node.lineno,
                "signature": f"class {node.name}",
                "doc": (ast.get_docstring(node) or "")[:200],
            })
        elif isinstance(node, ast.Import):
            imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return symbols, sorted(set(imports))


def _signature(node):
    args = [a.arg for a in node.args.posonlyargs + node.args.args]
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    args += [a.arg for a in node.args.kwonlyargs]
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    return f"{node.name}({', '.join(args)})"


def _parse_ts(source):
    symbols = []
    for match in TS_SYMBOL.finditer(source):
        name = next(g for g in match.groups() if g)
        line = source.count("\n", 0, match.start()) + 1
        symbols.append({
            "name": name, "kind": "function", "line": line,
            "signature": match.group(0).strip(), "doc": "",
        })
    return symbols, sorted(set(TS_IMPORT.findall(source)))


def _module_path(rel):
    """The name a file is imported by. A package entry point is its directory:
    `from flask import x` refers to `src/flask/__init__.py`, whose own stem
    ends in `__init__` and would otherwise match nothing."""
    path = Path(rel)
    if path.stem in ("__init__", "index"):
        return path.parent.as_posix()
    return path.with_suffix("").as_posix()


def _callers(files):
    """module path -> files that import it.

    Each import resolves to at most one file. Matching on the path tail alone
    lets a test fixture called `flask.py` absorb every `from flask import ...`
    in the repository and come out looking like the most depended-on module in
    the codebase, so when several files could satisfy an import we take the
    real one: source before tests, then the shortest path.
    """
    stems = {rel: _module_path(rel) for rel in files}
    index = {}
    for source, info in files.items():
        for imp in info["imports"]:
            normalized = imp.replace(".", "/").lstrip("./")
            if not normalized:
                continue
            candidates = [
                rel for rel, stem in stems.items()
                if rel != source and stem.endswith(normalized)
            ]
            if not candidates:
                continue
            best = min(candidates, key=lambda rel: (files[rel]["is_test"], len(rel)))
            index.setdefault(best, []).append(source)
    return {k: sorted(set(v)) for k, v in index.items()}
