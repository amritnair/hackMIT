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


# What a signal is worth when the words miss. Uncommitted and claimed files
# are about this minute; a recent commit is only about this week, so it is
# worth less and must not push the top eight full of git log.
HINT_WEIGHTS = {
    "editing": (4.5, "the agent said it is editing this file"),
    "dirty": (4.0, "uncommitted in this working tree"),
    "claimed": (3.5, "another session is already in this file"),
    "recent": (1.2, "changed in a recent commit"),
}


def _hint_map(repo, hints):
    """path -> [(weight, why)], for hint paths this repository actually has.

    Anything that is not a file here is dropped rather than guessed at: a
    stale path from git status is not evidence about code that exists.
    """
    found = {}
    for kind, (weight, why) in HINT_WEIGHTS.items():
        seen = set()
        for path in (hints or {}).get(kind) or ():
            # one signal counts once however many times it is reported; three
            # sessions in a file is worth saying, not worth scoring three times
            if path in repo["files"] and path not in seen:
                seen.add(path)
                found.setdefault(path, []).append((weight, why))
        if kind == "claimed":
            counts = {}
            for path in (hints or {}).get(kind) or ():
                if path in repo["files"]:
                    counts[path] = counts.get(path, 0) + 1
            for path, n in counts.items():
                if n > 1:
                    found[path] = [
                        (w, f"{n} other pieces of work are already in this file")
                        if text == why else (w, text)
                        for w, text in found[path]
                    ]
    return found


def predict(repo, task, limit=8, hints=None):
    """Which files this task is likely to touch.

    Lexical matching against the repository, plus optional signals the caller
    already knows: what is uncommitted, what changed recently, what another
    session has claimed. Those let a task land on the right file even when it
    shares no words with it. They never invent a path, and every one of them
    says why it was included.

    Without hints this behaves exactly as it did before, so callers that only
    hold a scanned repository dict do not have to find any of it.
    """
    task_tokens = tokens(task)
    hinted = _hint_map(repo, hints)
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
        lexical = score > 0
        if info["is_test"]:
            score *= 0.6  # tests follow the code, they rarely lead it
        for weight, why in hinted.get(rel, ()):
            score += weight
            evidence.append(why)
        if not score:
            continue
        scored.append({
            "file": rel,
            "score": round(score, 2),
            "symbols": symbols[:5],
            "evidence": evidence[:5],
            "is_test": info["is_test"],
            "callers": repo["callers"].get(rel, []),
            "lexical": lexical,
        })

    # a file the words actually matched sorts above one that only a signal
    # raised, when the two come out level
    scored.sort(key=lambda f: (-f["score"], not f["lexical"], f["file"]))
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
        "confidence": _confidence_with_signals(top),
        "unsupported_terms": sorted(t for t in task_tokens if not overlap({t}, grounded)),
    }


def _tests_near(repo, files):
    """Tests that import something we expect to change."""
    stems = {Path(f).stem for f in files}
    return sorted(
        rel for rel, info in repo["files"].items()
        if info["is_test"] and stems & {Path(i.replace(".", "/")).stem for i in info["imports"]}
    )[:5]


def _confidence_with_signals(top):
    """How much to trust this forecast, given what kind of evidence it rests on.

    Words matching code is the strong case: it says this task belongs in this
    file. A file being uncommitted or claimed is weaker but not nothing, and
    reporting zero next to a file the caller can see ranked first reads as a
    bug rather than as honesty. So signals set a floor and the words set the
    ceiling, and the floor stays below the point where anyone would act on it
    without looking.
    """
    lexical = _confidence([f for f in top if f["lexical"]])
    if any(not f["lexical"] for f in top):
        return max(lexical, 0.35)
    return lexical


def _confidence(top):
    """Deliberately blunt: strong top hit and a clear gap to second place."""
    if not top:
        return 0.0
    lead = top[0]["score"]
    runner_up = top[1]["score"] if len(top) > 1 else 0.0
    separation = (lead - runner_up) / lead
    return round(min(0.95, 0.3 + min(lead, 12) / 24 + separation * 0.2), 2)
