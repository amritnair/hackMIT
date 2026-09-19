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


def scan(repo):
    repo = Path(repo).resolve()
    sha = git(repo, "rev-parse", "HEAD")
    tracked = [p for p in git(repo, "ls-files").splitlines() if p]

    files = {}
    for rel in tracked:
        path = repo / rel
        if path.suffix not in CODE_SUFFIXES or not path.is_file():
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
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "files": files,
        "manifests": [p for p in tracked if Path(p).name in MANIFESTS],
        "schema_files": [p for p in tracked if _is_schema(p)],
        "callers": _callers(files),
    }


def _is_test(rel):
    name = Path(rel).name
    return (
        name.startswith("test_")
        or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts"))
        or "tests/" in rel
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


def _callers(files):
    """module path -> files that import it. Suffix match, so it is a superset."""
    index = {}
    for rel, info in files.items():
        stem = Path(rel).with_suffix("").as_posix()
        for other, other_info in files.items():
            if other == rel:
                continue
            for imp in other_info["imports"]:
                normalized = imp.replace(".", "/").lstrip("./")
                if normalized and stem.endswith(normalized):
                    index.setdefault(rel, []).append(other)
                    break
    return {k: sorted(set(v)) for k, v in index.items()}
