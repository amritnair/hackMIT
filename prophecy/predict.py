"""Guess which parts of the repo a task description will touch.

Lexical matching against real paths, symbol names and docstrings. Every hit
carries the evidence that produced it, so a wrong forecast is at least an
inspectable wrong forecast.
"""

import re
from pathlib import Path

STOPWORDS = {
    "add", "the", "a", "an", "to", "for", "and", "or", "of", "in", "on", "with",
    "make", "new", "support", "implement", "update", "fix", "change", "use",
    "from", "into", "our", "we", "should", "need", "that", "this", "it", "be",
}
# words that sound like code but never narrow anything down
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]+")


def tokens(text):
    out = set()
    for word in WORD.findall(text):
        for part in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", word) or [word]:
            part = part.lower()
            if len(part) > 2 and part not in STOPWORDS:
                out.add(part)
                out.add(part.rstrip("s"))
    return {t for t in out if len(t) > 2}


def overlap(a, b):
    """Tokens shared by two sets, allowing morphological variants.

    'scanner' should find scan.py and 'authentication' should find a function
    called authenticate. Exact set intersection finds neither, and stemming
    properly is not worth a dependency, so two words of four or more letters
    count as the same when they agree on their first four. That does misfire
    now and then, which is why every match is reported as evidence rather
    than acted on silently.
    """
    hits = set()
    for x in a:
        for y in b:
            if x == y or (len(x) >= 4 and len(y) >= 4 and x[:4] == y[:4]):
                hits.add(min(x, y, key=len))
    return hits


def predict(repo, task, limit=8):
    task_tokens = tokens(task)
    scored = []

    for rel, info in repo["files"].items():
        evidence = []
        path_hits = overlap(task_tokens, tokens(rel))
        for hit in sorted(path_hits):
            evidence.append(f"path {rel} contains '{hit}'")

        # Distinct matched words, not match events. Scoring per event lets a
        # 2000-line test file win on volume alone: forty functions whose
        # docstrings all say "request" is not forty pieces of evidence.
        symbol_hits, doc_hits, symbols = set(), set(), []
        for sym in info["symbols"]:
            matched = overlap(task_tokens, tokens(sym["name"]))
            if matched:
                symbol_hits |= matched
                symbols.append(sym)
                evidence.append(
                    f"symbol {sym['signature']} at {rel}:{sym['line']} "
                    f"matches '{', '.join(sorted(matched))}'"
                )
                continue
            in_doc = overlap(task_tokens, tokens(sym["doc"])) if sym["doc"] else set()
            if in_doc - doc_hits:
                doc_hits |= in_doc
                symbols.append(sym)
                evidence.append(f"docstring of {sym['name']} in {rel} mentions the task")

        score = (
            3.0 * len(path_hits)
            + 2.0 * min(len(symbol_hits), 3)
            + 0.5 * min(len(doc_hits), 2)
        )
        if not score:
            continue
        if info["is_test"]:
            score *= 0.6  # tests follow the code, they rarely lead it
        scored.append({
            "file": rel,
            "score": round(score, 2),
            "symbols": symbols[:5],
            "evidence": evidence[:5],
            "is_test": info["is_test"],
            "callers": repo["callers"].get(rel, []),
        })

    scored.sort(key=lambda f: (-f["score"], f["file"]))
    top = scored[:limit]

    grounded = set()
    for f in top:
        grounded |= tokens(f["file"])
        for sym in f["symbols"]:
            grounded |= tokens(sym["name"])

    return {
        "task": task,
        "sha": repo["sha"],
        "files": top,
        "tests": [f["file"] for f in top if f["is_test"]]
        or _tests_near(repo, [f["file"] for f in top]),
        "confidence": _confidence(top),
        "unsupported_terms": sorted(t for t in task_tokens if not overlap({t}, grounded)),
    }


def _tests_near(repo, files):
    """Tests that import something we expect to change."""
    stems = {Path(f).stem for f in files}
    return sorted(
        rel for rel, info in repo["files"].items()
        if info["is_test"] and stems & {Path(i.replace(".", "/")).stem for i in info["imports"]}
    )[:5]


def _confidence(top):
    """Deliberately blunt: strong top hit and a clear gap to second place."""
    if not top:
        return 0.0
    lead = top[0]["score"]
    runner_up = top[1]["score"] if len(top) > 1 else 0.0
    separation = (lead - runner_up) / lead
    return round(min(0.95, 0.3 + min(lead, 12) / 24 + separation * 0.2), 2)
