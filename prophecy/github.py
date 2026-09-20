"""Pull requests as another kind of work in flight.

Goes through the `gh` CLI rather than an HTTP client, so it reuses whatever
auth the person already has and adds no dependency. A pull request is just a
branch with a description attached, so once the refs are fetched the existing
risk engine does the work unchanged.
"""

import json
import subprocess
from pathlib import Path

from .branches import as_forecast, diff
from .scan import git


class GitHubUnavailable(RuntimeError):
    """gh is missing, not logged in, or the repo has no GitHub remote."""


def gh(repo, *args):
    try:
        out = subprocess.run(
            ["gh", *args, "--repo", slug(repo)] if slug(repo) else ["gh", *args],
            capture_output=True, text=True, cwd=str(repo),
        )
    except FileNotFoundError:
        raise GitHubUnavailable(
            "The GitHub CLI (gh) is not installed, so pull requests cannot be read.")
    if out.returncode:
        raise GitHubUnavailable(out.stderr.strip()[:300] or "gh failed")
    return out.stdout


def slug(repo):
    """owner/name from the origin remote, or None if there isn't one."""
    try:
        url = git(repo, "remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        return None
    url = url.removesuffix(".git")
    if url.startswith("git@"):
        url = url.split(":", 1)[-1]
    parts = [p for p in url.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def pull_requests(repo, state="open", limit=20):
    repo = Path(repo).resolve()
    if not slug(repo):
        raise GitHubUnavailable("No GitHub remote named origin.")
    raw = gh(
        repo, "pr", "list", "--state", state, "--limit", str(limit),
        "--json", "number,title,author,headRefName,baseRefName,isDraft,updatedAt",
    )
    return json.loads(raw or "[]")


def fetch(repo, number):
    """Bring a PR head into a local ref we own, without touching the checkout.

    `gh pr checkout` would switch branches under the person; this just puts
    the commits where we can diff them.
    """
    repo = Path(repo).resolve()
    ref = f"refs/prophecy/pr-{number}"
    subprocess.run(
        ["git", "-C", str(repo), "fetch", "-q", "origin",
         f"pull/{number}/head:{ref}", "--force"],
        capture_output=True, text=True, check=True,
    )
    return ref


def forecast(repo, pr):
    """A PR, shaped like anything else the risk engine takes."""
    repo = Path(repo).resolve()
    ref = fetch(repo, pr["number"])
    base = pr.get("baseRefName") or "main"
    try:
        fork = git(repo, "merge-base", f"origin/{base}", ref)
    except subprocess.CalledProcessError:
        fork = git(repo, "merge-base", base, ref)

    label = f"PR #{pr['number']}: {pr['title']}"
    observed = diff(repo, fork, ref, label=label)
    shaped = as_forecast(observed)
    shaped["pull_request"] = {
        "number": pr["number"],
        "title": pr["title"],
        "author": (pr.get("author") or {}).get("login", "unknown"),
        "base": base,
        "draft": pr.get("isDraft", False),
    }
    return shaped


def comment_body(number, risks, sha):
    """The comment prophecy would leave, if asked to leave one."""
    mine = [r for r in risks if any(f"#{number}" in t for t in r["tasks"])]
    lines = [
        "## prophecy",
        "",
        f"Checked against the other open pull requests at `{sha[:10]}`.",
        "",
    ]
    if not mine:
        lines.append(
            "No overlap with any other open pull request. This is about files "
            "and symbols, not behaviour, so a clean report here is not a promise "
            "that nothing breaks."
        )
        return "\n".join(lines)

    for r in mine:
        others = [t for t in r["tasks"] if f"#{number}" not in t]
        lines.append(
            f"**{r['risk_level']}** · `{r['risk_type']}`"
            + (f" · against {', '.join(others)}" if others else "")
        )
        for line in r["evidence"]:
            lines.append(f"- {line}")
        lines.append(f"\n> {r['recommendation']}\n")

    lines.append(
        "\nThese are predicted risks, from the structure of the two changes. "
        "Nothing has been merged or run."
    )
    return "\n".join(lines)


def post_comment(repo, number, body):
    """Only ever called when somebody passed --comment. Writes to the PR."""
    repo = Path(repo).resolve()
    out = subprocess.run(
        ["gh", "pr", "comment", str(number), "--body", body,
         "--repo", slug(repo)],
        capture_output=True, text=True, cwd=str(repo),
    )
    if out.returncode:
        raise GitHubUnavailable(out.stderr.strip()[:300])
    return out.stdout.strip()
