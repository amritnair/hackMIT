"""What the branches in this repo are actually doing, as opposed to what
somebody says they are about to do.

Everything here is observed from git history, so it is evidence rather than
forecast. It is what predictions get checked against.
"""

import subprocess
from pathlib import Path

from .scan import CODE_SUFFIXES, _parse_python, _parse_ts, git


def _show(repo, rev, path):
    out = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        capture_output=True, text=True,
    )
    return out.stdout if out.returncode == 0 else None


def _symbols(path, source):
    parse = _parse_python if Path(path).suffix == ".py" else _parse_ts
    return {s["name"]: s for s in parse(source)[0]}


def branches(repo_path, base="main", include_base=False):
    repo = Path(repo_path).resolve()
    names = [
        line.strip() for line in
        git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
        if line.strip()
    ]
    if base not in names:
        base = names[0] if names else base
    return {
        name: branch(repo, name, base)
        for name in names
        if include_base or name != base
    }


def branch(repo, name, base):
    repo = Path(repo)
    try:
        fork = git(repo, "merge-base", base, name)
    except subprocess.CalledProcessError:
        return {"name": name, "error": f"no common ancestor with {base}"}

    counts = git(repo, "rev-list", "--left-right", "--count", f"{base}...{name}")
    behind, ahead = (int(n) for n in counts.split())
    changed = [
        p for p in git(repo, "diff", "--name-only", f"{fork}..{name}").splitlines() if p
    ]

    signature_changes, touched_symbols = [], {}
    for path in changed:
        if Path(path).suffix not in CODE_SUFFIXES:
            continue
        before, after = _show(repo, fork, path), _show(repo, name, path)
        old = _symbols(path, before) if before else {}
        new = _symbols(path, after) if after else {}
        touched_symbols[path] = sorted(set(old) | set(new))
        for sym in sorted(set(old) & set(new)):
            if old[sym]["signature"] != new[sym]["signature"]:
                signature_changes.append({
                    "file": path, "symbol": sym,
                    "before": old[sym]["signature"], "after": new[sym]["signature"],
                })
        for sym in sorted(set(old) - set(new)):
            signature_changes.append({
                "file": path, "symbol": sym,
                "before": old[sym]["signature"], "after": "(removed)",
            })

    return {
        "name": name,
        "base": base,
        "fork_point": fork,
        "head": git(repo, "rev-parse", name),
        "ahead": ahead,
        "behind": behind,
        "author": git(repo, "log", "-1", "--format=%an", name),
        "last_commit": git(repo, "log", "-1", "--format=%ar", name),
        "subject": git(repo, "log", "-1", "--format=%s", name),
        "changed_files": changed,
        "touched_symbols": touched_symbols,
        "signature_changes": signature_changes,
    }


def as_forecast(info):
    """Dress an observed branch up as a forecast so the risk engine can take it.

    The evidence says 'changed' rather than 'forecast to touch', because this
    part is not a guess.
    """
    files = []
    for path in info["changed_files"]:
        symbols = [
            {"name": s, "signature": s, "line": 0, "kind": "symbol", "doc": ""}
            for s in info["touched_symbols"].get(path, [])
        ]
        files.append({
            "file": path,
            "score": 10.0,
            "symbols": symbols,
            "evidence": [f"branch {info['name']} changed {path}"],
            "is_test": "test" in path.lower(),
            "callers": [],
        })
    return {
        "task": f"branch:{info['name']}",
        "sha": info["head"],
        "files": files,
        "tests": [f["file"] for f in files if f["is_test"]],
        "confidence": 1.0,
        "unsupported_terms": [],
        "observed": True,
        "signature_changes": info["signature_changes"],
        "ahead": info["ahead"],
        "behind": info["behind"],
    }
