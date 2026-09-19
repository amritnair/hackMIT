"""Actually merge two branches, in a throwaway worktree, and see what happens.

This is the other half of the pitch. A forecast nobody checks is a horoscope.
Everything happens in a temporary worktree under the system temp directory —
the working copy you are sitting in is never touched, never checked out, and
never merged into.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from .scan import git


def _run(cwd, *args, timeout=120):
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
    )


def trial_merge(repo_path, base, branch, test_cmd=None):
    """Merge branch into base in a scratch worktree. Returns what happened."""
    repo = Path(repo_path).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="mergemind-"))
    work = tmp / "w"
    result = {
        "base": base, "branch": branch,
        "base_sha": git(repo, "rev-parse", base),
        "branch_sha": git(repo, "rev-parse", branch),
        "conflicted_files": [], "merged_clean": False,
        "tests": None, "error": None,
    }
    try:
        add = _run(repo, "git", "worktree", "add", "--detach", str(work), base)
        if add.returncode:
            result["error"] = add.stderr.strip()[:400]
            return result

        merge = _run(work, "git", "merge", "--no-commit", "--no-ff", branch)
        conflicts = _run(work, "git", "diff", "--name-only", "--diff-filter=U")
        result["conflicted_files"] = [p for p in conflicts.stdout.splitlines() if p]
        result["merged_clean"] = merge.returncode == 0 and not result["conflicted_files"]
        if not result["merged_clean"] and not result["conflicted_files"]:
            result["error"] = merge.stderr.strip()[:400]

        if result["merged_clean"] and test_cmd:
            try:
                tests = _run(work, *test_cmd, timeout=300)
                result["tests"] = {
                    "command": " ".join(test_cmd),
                    "passed": tests.returncode == 0,
                    "output": (tests.stdout + tests.stderr)[-2000:],
                }
            except subprocess.TimeoutExpired:
                result["tests"] = {
                    "command": " ".join(test_cmd), "passed": False,
                    "output": "timed out after 300s",
                }
    finally:
        _run(work, "git", "merge", "--abort")
        _run(repo, "git", "worktree", "remove", "--force", str(work))
        shutil.rmtree(tmp, ignore_errors=True)
    return result


def compare(predicted, outcome):
    """Score the forecast against the merge that actually ran.

    A predicted file that did not conflict is not automatically a false alarm:
    most coordination risks are semantic and never produce a text conflict.
    So these are counted separately and never averaged into one number.
    """
    predicted_files = sorted({
        line.split()[-1].rstrip(".,")
        for r in predicted for line in r["evidence"]
        if " touch " in line or " changed " in line or " edit " in line
    })
    actual = set(outcome["conflicted_files"])
    hit = sorted(set(predicted_files) & actual)
    missed = sorted(actual - set(predicted_files))
    quiet = sorted(set(predicted_files) - actual)

    notes = []
    if outcome["merged_clean"]:
        notes.append(
            "Merged cleanly. Text conflicts were never the claim — the predicted "
            "risks above are about behaviour, and this run does not clear them."
        )
    if outcome.get("tests") and not outcome["tests"]["passed"]:
        notes.append("Merge was clean but tests failed: a semantic conflict got through.")
    if missed:
        notes.append(f"{len(missed)} file(s) conflicted that nothing predicted.")
    return {
        "predicted_files": predicted_files,
        "conflicted_files": sorted(actual),
        "predicted_and_conflicted": hit,
        "conflicted_unpredicted": missed,
        "predicted_no_conflict": quiet,
        "merged_clean": outcome["merged_clean"],
        "tests_passed": (outcome.get("tests") or {}).get("passed"),
        "notes": notes,
    }
