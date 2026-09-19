"""Compare two or more forecasts and report where they collide.

These are predicted risks. Nothing here has been merged or run, so a high
score means "two people should talk", not "this will break".
"""

import hashlib
from itertools import combinations
from pathlib import Path

STALE_BEHIND = 20  # commits behind base before divergence is worth mentioning

LEVELS = ((0.66, "high"), (0.33, "medium"), (0.0, "low"))


def _level(score):
    return next(name for cutoff, name in LEVELS if score >= cutoff)


def risks(repo, forecasts):
    found = []
    for forecast in forecasts:
        found += _solo_risks(repo, forecast)
    for a, b in combinations(forecasts, 2):
        found += _pair_risks(repo, a, b)
    found.sort(key=lambda r: -r["risk_score"])
    return found


def _solo_risks(repo, forecast):
    """Risks visible in one branch on its own, without a second task to hit."""
    out = []
    # Group by file first. A branch that moves twelve classes out of one module
    # has done one thing, not twelve, and reporting it twelve times is how a
    # warning list gets ignored.
    by_file = {}
    for change in forecast.get("signature_changes", []):
        by_file.setdefault(change["file"], []).append(change)

    for path, changes in sorted(by_file.items()):
        callers = repo["callers"].get(path, [])
        moved = [c for c in changes if c.get("kind") == "moved"]
        edited = [c for c in changes if c.get("kind") != "moved"]

        if len(moved) >= 3:
            targets = sorted({c["moved_to"] for c in moved if c["moved_to"]})
            out.append(_risk(
                "symbols_relocated", 0.5 + min(0.35, 0.05 * len(callers)),
                [
                    f"{len(moved)} symbol(s) moved out of {path} into "
                    f"{', '.join(targets[:3])}",
                    "moved, not deleted: " + ", ".join(
                        c["symbol"] for c in moved[:6]),
                    f"{len(callers)} file(s) import {path}: "
                    f"{', '.join(callers[:4])}" if callers
                    else f"no tracked file imports {path}",
                ],
                f"Import sites for {path} need updating, and anything else "
                "editing this module will conflict with the move. Land it "
                "first or hold it.",
                (forecast["task"],), repo, files=[path],
            ))
            edited += [c for c in moved if False]  # moves are reported above
        else:
            edited += moved

        for change in edited:
            score = 0.4 + min(0.4, 0.08 * len(callers))
            evidence = [
                f"{change['symbol']} in {path} changed from "
                f"{change['before']} to {change['after']}",
            ]
            evidence.append(
                f"{len(callers)} file(s) import {path}: {', '.join(callers[:4])}"
                if callers else f"no tracked file imports {path}"
            )
            out.append(_risk(
                "api_signature_change", score, evidence,
                f"Check the {len(callers)} caller(s) of {change['symbol']} "
                "before merging." if callers else
                f"Signature of {change['symbol']} changed; no callers found "
                "in this repo.",
                (forecast["task"],), repo, files=[path],
            ))

    behind = forecast.get("behind", 0)
    if behind >= STALE_BEHIND:
        out.append(_risk(
            "branch_divergence", min(0.3 + behind / 200, 0.7),
            [f"{forecast['task']} is {behind} commit(s) behind its base "
             f"and {forecast.get('ahead', 0)} ahead"],
            "Rebase before this drifts further; the merge gets harder from here.",
            (forecast["task"],), repo,
        ))
    return out


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

        if rel in repo["regenerated_files"]:
            score = 0.7
            kind = "regenerated_file_overlap"
            evidence.append(
                f"{rel} is a lockfile, CI config or changelog — the kind of file "
                "two branches collide in most often, and for the least "
                "interesting reasons"
            )
            recommendation = (
                "Do not hand-merge this. Take one side, then regenerate or "
                "re-append after rebasing."
            )

        if rel in repo["schema_files"]:
            score += 0.2
            kind = "schema_overlap"
            evidence.append(f"{rel} looks like schema or migration code")
            recommendation = "Land one migration first; concurrent migrations collide."

        out.append(_risk(kind, score, evidence, recommendation, pair, repo,
                         files=[rel]))

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
        pair, repo, files=shared,
    )]


def _test_risk(a, b, pair, repo):
    shared = set(a["tests"]) & set(b["tests"])
    if not shared:
        return []
    return [_risk(
        "test_impact", 0.3,
        [f"both tasks depend on {rel}" for rel in sorted(shared)],
        "Expect churn in shared tests; whoever lands second reruns them.",
        pair, repo, files=shared,
    )]


def _risk(kind, score, evidence, recommendation, pair, repo, files=()):
    score = round(min(score, 0.95), 2)
    # id is a hash of what the risk is about, so the same risk keeps the same
    # id between runs and `prophecy explain <id>` stays valid.
    seed = "|".join([kind, *sorted(pair), evidence[0], repo["sha"]])
    return {
        "id": "R" + hashlib.blake2s(seed.encode(), digest_size=3).hexdigest(),
        "risk_type": kind,
        "files": sorted(files),
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
    # only risks between two pieces of work can be sequenced; a signature
    # change on one branch is not fixed by running it earlier
    orderable = [r for r in high if len(r["tasks"]) == 2]
    contested = sorted({f for r in found if len(r["tasks"]) == 2 for f in r["files"]})
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
                f"land '{r['tasks'][0]}' before '{r['tasks'][1]}'" for r in orderable
            ] or ["nothing here can be fixed by ordering alone"],
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
            other = [t for t in r["tasks"] if t != forecast["task"]]
            against = f' against "{other[0]}"' if other else ""
            lines.append(
                f"- **{r['risk_level']}** ({r['risk_type']}){against}: "
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
