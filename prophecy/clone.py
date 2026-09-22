"""Fetch a repository by URL, so trying this on your own code is a paste.

Pointing Prophecy at a project means having that project on disk. That is
fine when it is already there and a wall when it is not: a reader who wants
to see this against their own code should not have to work out a bind mount
first.

What this will not do is clone from anywhere. A URL is a request to write to
this machine's disk and talk to whatever host it names, so local paths, file
URLs and unknown schemes are refused rather than resolved. A public instance
refuses the whole endpoint, because filling someone else's disk should not
be a feature strangers have.
"""

import os
import re
import subprocess
from pathlib import Path

# Enough to name a repository, and nothing that is a path on this machine.
HTTPS = re.compile(r"^https://[\w.-]+(:\d+)?/[\w.\-/]+?(\.git)?/?$")
SSH = re.compile(r"^(git@|ssh://git@)[\w.-]+[:/][\w.\-/]+?(\.git)?/?$")
SHORTHAND = re.compile(r"^(github\.com|gitlab\.com|bitbucket\.org)/[\w.-]+/[\w.-]+$")
OWNER_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")

CLONE_TIMEOUT = 300


def workspace():
    """Where clones land. Configurable, because a container wants its own."""
    return Path(os.environ.get("PROPHECY_WORKSPACE",
                               Path.home() / "prophecy-repos")).expanduser()


def normalise(url):
    """The URL to clone, or None if this is not one we will take."""
    url = (url or "").strip()
    if not url or any(c.isspace() for c in url):
        return None
    if SHORTHAND.match(url):
        return "https://" + url
    if OWNER_REPO.match(url) and not url.startswith("."):
        # "owner/repo" means GitHub to almost everyone who types it
        return "https://github.com/" + url
    if HTTPS.match(url) or SSH.match(url):
        return url
    return None


def clone(url):
    """Clone into the workspace and return where it landed.

    A repository already there is updated rather than cloned again, so
    pasting the same URL twice is not an error.
    """
    target = normalise(url)
    if not target:
        return {"error": (
            "That does not look like a repository URL. Try "
            "https://github.com/owner/project, or owner/project.")}

    name = re.sub(r"\.git$", "", target.rstrip("/").split("/")[-1])
    if not name or name in (".", ".."):
        return {"error": "Could not work out a name from that URL."}

    root = workspace()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / name

    if (dest / ".git").is_dir():
        subprocess.run(["git", "-C", str(dest), "fetch", "--all", "--quiet"],
                       capture_output=True, timeout=CLONE_TIMEOUT)
        _local_branches(dest)
        return {"path": str(dest), "name": name, "already_here": True,
                "branches": _branch_count(dest)}

    done = subprocess.run(
        ["git", "clone", "--quiet", target, str(dest)],
        capture_output=True, text=True, timeout=CLONE_TIMEOUT,
        # a private repo would otherwise sit waiting for a password nobody
        # is there to type
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        message = detail[-1] if detail else "git could not clone that"
        if "Authentication failed" in message or "could not read" in message:
            message = ("That repository is private, or needs credentials this "
                       "machine does not have.")
        return {"error": message[:300]}

    _local_branches(dest)
    return {"path": str(dest), "name": name, "already_here": False,
            "branches": _branch_count(dest)}


def _local_branches(dest):
    """Give every remote branch a local one.

    Work in flight is read from local branches. A fresh clone has exactly
    one, so without this a repository with six branches in progress looks
    like a repository where nothing is happening.
    """
    listed = subprocess.run(
        ["git", "-C", str(dest), "branch", "-r", "--format=%(refname:short)"],
        capture_output=True, text=True)
    for ref in listed.stdout.split():
        if "HEAD" in ref or "/" not in ref:
            continue
        branch = ref.split("/", 1)[1]
        subprocess.run(
            ["git", "-C", str(dest), "branch", "--track", branch, ref],
            capture_output=True)


def _branch_count(dest):
    listed = subprocess.run(["git", "-C", str(dest), "branch"],
                            capture_output=True, text=True)
    return len([b for b in listed.stdout.splitlines() if b.strip()])
