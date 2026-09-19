"""Compare two or more forecasts and report where they collide.

These are predicted risks. Nothing here has been merged or run, so a high
score means "two people should talk", not "this will break".
"""

from itertools import combinations
from pathlib import Path

LEVELS = ((0.66, "high"), (0.33, "medium"), (0.0, "low"))


def _level(score):
    return next(name for cutoff, name in LEVELS if score >= cutoff)


def risks(repo, forecasts):
    found = []
    for a, b in combinations(forecasts, 2):
        found += _pair_risks(repo, a, b)
    found.sort(key=lambda r: -r["risk_score"])
    for i, risk in enumerate(found, 1):
        risk["id"] = f"R{i}"
    return found


def _pair_risks(repo, a, b):
    pair = (a["task"], b["task"])
    a_files = {f["file"]: f for f in a["files"]}
    b_files = {f["file"]: f for f in b["files"]}
    out = []

    for rel in sorted(set(a_files) & set(b_files)):
        evidence = [
            f"both tasks are forecast to touch {rel}",
            f"'{a['task']}' evidence: {a_files[rel]['evidence'][0]}",
            f"'{b['task']}' evidence: {b_files[rel]['evidence'][0]}",
        ]
        score = 0.45
        kind = "shared_file"
        recommendation = f"Agree on who owns {rel}, or sequence the two tasks."

        shared_symbols = {s["name"] for s in a_files[rel]["symbols"]} & {
            s["name"] for s in b_files[rel]["symbols"]
        }
        if shared_symbols:
            score += 0.25
            kind = "shared_symbol"
            evidence.append(f"same symbols in scope: {', '.join(sorted(shared_symbols))}")
            recommendation = (
                f"Settle the signature of {', '.join(sorted(shared_symbols))} "
                "before either task starts."
            )

        callers = repo["callers"].get(rel, [])
        if callers:
            score += min(0.2, 0.04 * len(callers))
            evidence.append(
                f"{len(callers)} file(s) import {rel}: {', '.join(callers[:4])}"
            )
            if shared_symbols:
                kind = "shared_api_contract"

        if rel in repo["schema_files"]:
            score += 0.2
            kind = "schema_overlap"
            evidence.append(f"{rel} looks like schema or migration code")
            recommendation = "Land one migration first; concurrent migrations collide."

        out.append(_risk(kind, score, evidence, recommendation, pair, repo))

    out += _manifest_risk(repo, a, b, pair)
    out += _test_risk(a, b, pair, repo)
    return out


def _manifest_risk(repo, a, b, pair):
    a_hits = {f["file"] for f in a["files"]} & set(repo["manifests"])
    b_hits = {f["file"] for f in b["files"]} & set(repo["manifests"])
    shared = a_hits & b_hits
    if not shared:
        return []
    return [_risk(
        "dependency_collision", 0.5,
        [f"both tasks are forecast to edit {rel}" for rel in sorted(shared)],
        "Add dependencies in one branch and rebase the other onto it.",
        pair, repo,
    )]


def _test_risk(a, b, pair, repo):
    shared = set(a["tests"]) & set(b["tests"])
    if not shared:
        return []
    return [_risk(
        "test_impact", 0.3,
        [f"both tasks depend on {rel}" for rel in sorted(shared)],
        "Expect churn in shared tests; whoever lands second reruns them.",
        pair, repo,
    )]


def _risk(kind, score, evidence, recommendation, pair, repo):
    score = round(min(score, 0.95), 2)
    return {
        "risk_type": kind,
        "risk_score": score,
        "risk_level": _level(score),
        "tasks": list(pair),
        "evidence": evidence,
        "recommendation": recommendation,
        "confidence": round(min(0.85, 0.4 + 0.1 * len(evidence)), 2),
        "repository_sha": repo["sha"],
    }


def strategies(repo, forecasts, found):
    """Same work, different orderings. No claim that one is optimal."""
    high = [r for r in found if r["risk_level"] == "high"]
    contested = sorted({
        line.split()[-1] for r in found for line in r["evidence"]
        if line.startswith("both tasks are forecast to touch")
    })
    return [
        {
            "strategy": "all in parallel",
            "open_risks": len(found),
            "coordination_steps": [],
            "note": f"{len(high)} high risk(s) resolved at merge time instead of now.",
        },
        {
            "strategy": "sequence by overlap",
            "open_risks": max(0, len(found) - len(high)),
            "coordination_steps": [
                f"land '{r['tasks'][0]}' before '{r['tasks'][1]}'" for r in high
            ],
            "note": "Serialises the contested work; the later task rebases.",
        },
        {
            "strategy": "agree interfaces first",
            "open_risks": max(0, len(found) - len(high)),
            "coordination_steps": [
                f"freeze the interface in {rel}" for rel in contested[:4]
            ] or ["nothing contested to freeze"],
            "note": "Both tasks proceed in parallel once the shared edges are fixed.",
        },
    ]


def capsule(repo, forecast, found):
    """Markdown briefing for whoever (or whatever) picks up the task."""
    lines = [
        f"# Context: {forecast['task']}",
        "",
        f"Repo `{Path(repo['repo']).name}` at `{repo['sha'][:10]}` "
        f"(branch `{repo['branch']}`). Forecast confidence {forecast['confidence']}.",
        "",
        "## Files you will probably touch",
    ]
    for f in forecast["files"]:
        lines.append(f"- `{f['file']}` — {f['evidence'][0]}")
        for sym in f["symbols"][:3]:
            lines.append(f"  - `{sym['signature']}` (line {sym['line']})")
        if f["callers"]:
            lines.append(f"  - imported by {len(f['callers'])} file(s)")

    if forecast["tests"]:
        lines += ["", "## Tests in scope"] + [f"- `{t}`" for t in forecast["tests"]]

    mine = [r for r in found if forecast["task"] in r["tasks"]]
    if mine:
        lines += ["", "## Coordination"]
        for r in mine:
            other = [t for t in r["tasks"] if t != forecast["task"]][0]
            lines.append(
                f"- **{r['risk_level']}** ({r['risk_type']}) against \"{other}\": "
                f"{r['recommendation']}"
            )

    if forecast["unsupported_terms"]:
        lines += [
            "",
            "## Not grounded in the repo",
            "These words from the task matched nothing that exists today: "
            + ", ".join(f"`{t}`" for t in forecast["unsupported_terms"])
            + ". Either they are new concepts, or the forecast missed something.",
        ]
    return "\n".join(lines)
