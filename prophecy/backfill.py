"""Replay merges that already happened and grade the forecast against them.

For each historical merge commit, we rewind to the point where the two sides
diverged, scan the repo as it was then, forecast from the two sets of changes,
and then re-run the merge for real to see what git makes of it. The forecast
never sees the merge commit, so it cannot cheat.

This is the only honest source of accuracy numbers in the project. Everything
else is a heuristic asserting confidence in itself.
"""

import statistics
import subprocess
from pathlib import Path

from .branches import as_forecast, diff
from .merge import trial_merge
from .risk import risks
from .scan import CODE_SUFFIXES, git, scan
from .text import plural


def merge_commits(repo, limit, ref="HEAD"):
    """Two-parent merges, newest first. Octopus merges are skipped."""
    log = git(repo, "log", ref, "--merges", "--format=%H %ct %P", f"-n{limit * 2}")
    out = []
    for line in log.splitlines():
        parts = line.split()
        if len(parts) != 4:  # sha, time, and exactly two parents
            continue
        out.append({"sha": parts[0], "time": int(parts[1]),
                    "base": parts[2], "branch": parts[3]})
        if len(out) >= limit:
            break
    return out


def replay_one(repo_path, merge):
    repo = Path(repo_path)
    try:
        fork = git(repo, "merge-base", merge["base"], merge["branch"])
    except subprocess.CalledProcessError:
        return None

    # The world as it was before either side started. No peeking.
    state = scan(repo, rev=fork)
    sides = [
        as_forecast(diff(repo, fork, merge["base"], label=f"side:{merge['base'][:8]}")),
        as_forecast(diff(repo, fork, merge["branch"], label=f"side:{merge['branch'][:8]}")),
    ]
    if not any(s["files"] for s in sides):
        return None  # nothing changed in code files; nothing to forecast

    predicted = risks(state, sides)
    # The files the risk engine actually points at. Not the union of everything
    # both sides touched: that set contains every conflict by construction, so
    # scoring against it would be a tautology dressed up as a recall figure.
    predicted_files = {f for r in predicted for f in r["files"]}
    touched = {f["file"] for s in sides for f in s["files"]}

    # highest level any risk assigned to each file, so we can ask the only
    # question that actually tests the scoring: does a louder warning mean a
    # higher chance of trouble?
    rank = {"low": 0, "medium": 1, "high": 2}
    levels = {}
    for r in predicted:
        for f in r["files"]:
            if rank[r["risk_level"]] >= rank.get(levels.get(f, "low"), -1):
                levels[f] = r["risk_level"]

    outcome = trial_merge(repo, merge["base"], merge["branch"])
    if outcome["error"]:
        return None
    conflicted = set(outcome["conflicted_files"])

    fork_time = int(git(repo, "show", "-s", "--format=%ct", fork))
    return {
        "merge": merge["sha"],
        "conflicted": sorted(conflicted),
        "predicted_files": sorted(predicted_files),
        "files_in_play": len(touched),
        "levels": levels,
        "caught": sorted(conflicted & predicted_files),
        "missed": sorted(conflicted - predicted_files),
        "flagged_no_conflict": sorted(predicted_files - conflicted),
        "risks": len(predicted),
        "high_risks": sum(1 for r in predicted if r["risk_level"] == "high"),
        "lead_seconds": merge["time"] - fork_time,
    }


def replay(repo_path, limit=50, ref="HEAD", progress=None):
    runs = []
    for merge in merge_commits(Path(repo_path), limit, ref):
        result = replay_one(repo_path, merge)
        if result:
            runs.append(result)
            if progress:
                progress(result)
    return runs


def report(runs, minimum=10):
    """Aggregate, and say plainly which numbers are worth anything.

    Recall on text conflicts is close to meaningless here and is reported with
    that caveat attached: a text conflict requires both sides to edit the same
    file, which is the exact condition that makes this tool fire. Catching
    them all is arithmetic, not skill.

    The number that does test the scoring is the conflict rate by risk level.
    If 'high' does not conflict more often than 'low', the score is decoration.
    """
    conflicting = [r for r in runs if r["conflicted"]]
    caught = sum(len(r["caught"]) for r in conflicting)
    missed = sum(len(r["missed"]) for r in conflicting)
    flagged_quiet = sum(len(r["flagged_no_conflict"]) for r in runs)
    leads = [r["lead_seconds"] / 3600 for r in runs if r["lead_seconds"] > 0]

    # Split code from everything else before comparing levels. Lockfiles, CI
    # config and changelogs conflict constantly and for reasons that have
    # nothing to do with how risky the work was; leaving them in the same
    # bucket as source files makes the scoring look inverted when it is not.
    by_level = {}
    for run in runs:
        conflicted = set(run["conflicted"])
        for path, level in run["levels"].items():
            kind = "code" if Path(path).suffix in CODE_SUFFIXES else "non-code"
            bucket = by_level.setdefault(f"{level} ({kind})",
                                         {"flagged": 0, "conflicted": 0})
            bucket["flagged"] += 1
            bucket["conflicted"] += path in conflicted
    for bucket in by_level.values():
        bucket["rate"] = round(bucket["conflicted"] / bucket["flagged"], 3)

    out = {
        "merges_replayed": len(runs),
        "merges_with_conflicts": len(conflicting),
        "files_in_play": sum(r["files_in_play"] for r in runs),
        "files_flagged": sum(len(r["predicted_files"]) for r in runs),
        "conflicted_files": caught + missed,
        "files_caught": caught,
        "files_missed": missed,
        "files_flagged_that_merged_clean": flagged_quiet,
        "median_lead_hours": round(statistics.median(leads), 1) if leads else None,
        "by_level": dict(sorted(
            by_level.items(),
            key=lambda kv: (kv[0].endswith("(code)") is False,
                            -{"high": 2, "medium": 1, "low": 0}[kv[0].split()[0]]))),
        "sample_too_small": len(conflicting) < minimum,
    }
    out["recall_note"] = (
        f"{caught} of {caught + missed} conflicting files were flagged in "
        "advance, but treat that as a sanity check rather than a score: a text "
        "conflict requires both sides to touch one file, which is precisely "
        "when this fires. Missing one would indicate a bug."
    )
    if out["sample_too_small"]:
        out["verdict"] = (
            f"Only {plural(len(conflicting), 'conflicting merge')} replayed. "
            f"Below {minimum} the rates below are anecdote, not evidence."
        )
    else:
        # compare the loudest and quietest levels among code files only
        present = [f"{lvl} (code)" for lvl in ("high", "medium", "low")
                   if f"{lvl} (code)" in by_level]
        if len(present) >= 2:
            loud, quiet = present[0], present[-1]
            loud_rate, quiet_rate = by_level[loud]["rate"], by_level[quiet]["rate"]
            direction = (
                f"Among source files, {loud} conflicted {loud_rate * 100:.1f}% "
                f"of the time against {quiet} at {quiet_rate * 100:.1f}%, so the "
                "ordering holds."
                if loud_rate > quiet_rate else
                f"The score is inverted, even after separating source files from "
                f"generated ones: {loud} conflicted {loud_rate * 100:.1f}% of the "
                f"time while {quiet} hit {quiet_rate * 100:.1f}%. "
                "Do not trust the ordering until this is resolved."
            )
        else:
            direction = "Only one risk level appeared among source files."
        out["verdict"] = (
            f"Across {plural(len(conflicting), 'conflicting merge')}: "
            f"{plural(out['files_flagged'], 'file')} flagged out of "
            f"{out['files_in_play']} touched. {direction}"
        )
    return out
