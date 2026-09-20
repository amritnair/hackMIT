"""Who changed what, and putting a file back the way it was.

Restoring is deliberately the least destructive thing that counts as a
restore: it writes old content into the working tree and stops. No reset, no
history rewrite, no force push. Whatever it does is visible in `git diff` and
undone by `git checkout -- .`, which is the only kind of undo worth offering
somebody through a web page.
"""

import re
import subprocess
from pathlib import Path

from .scan import git
from .text import pick, plural


def commits(repo_path, limit=40, path=None):
    """Recent commits, with who wrote them and what they touched."""
    args = ["log", "--all", f"-{limit}",
            "--format=%H%x1f%an%x1f%ae%x1f%ar%x1f%s"]
    if path:
        args += ["--", path]
    try:
        raw = git(repo_path, *args)
    except subprocess.CalledProcessError:
        return []
    out = []
    for line in raw.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        sha, author, email, when, subject = parts
        try:
            files = [f for f in git(repo_path, "show", "--name-only",
                                    "--format=", sha).splitlines() if f]
        except subprocess.CalledProcessError:
            files = []
        out.append({
            "sha": sha, "short": sha[:10], "author": author, "email": email,
            "when": when, "subject": subject, "files": files,
        })
    return out


def porcelain_path(line):
    """The path out of a `git status --porcelain` line.

    The status column is one or two characters and may be padded, and the
    first line arrives already left-stripped because git output is trimmed
    before it gets here. A rename reads "R old -> new"; the new name is the
    one that exists.
    """
    body = re.sub(r"^\s*[A-Z?!]{0,2}\s+", "", line.rstrip())
    return body.split(" -> ")[-1].strip().strip('"')


def dirty_paths(repo_path):
    """Repo-relative paths with uncommitted work."""
    return [porcelain_path(line) for line in dirty(repo_path)]


def dirty(repo_path):
    """Uncommitted work that a restore would sit on top of."""
    try:
        return [line for line in
                git(repo_path, "status", "--porcelain").splitlines() if line]
    except subprocess.CalledProcessError:
        return []


def preview_restore(repo_path, sha, paths=None):
    """What would change if this version were put back, without doing it."""
    targets = paths or []
    try:
        args = ["diff", "--stat", f"{sha}", "--"] + targets if targets else \
               ["diff", "--stat", f"{sha}"]
        stat = git(repo_path, *args)
    except subprocess.CalledProcessError as exc:
        return {"error": (exc.stderr or "").strip()[:200] or "cannot read that commit"}
    return {
        "sha": sha,
        "paths": targets,
        "diffstat": stat,
        "uncommitted": dirty(repo_path),
    }


def restore(repo_path, sha, paths=None, force=False):
    """Write an old version back into the working tree. Nothing else.

    Refuses when there is uncommitted work unless told otherwise, because the
    restore would land on top of it and the two would be hard to tell apart.
    """
    outstanding = dirty(repo_path)
    if outstanding and not force:
        return {
            "error": (
                f"{plural(len(outstanding), 'file')} {pick(len(outstanding), 'has', 'have')} uncommitted changes. Commit "
                "or stash them first, or restore anyway if you are sure."
            ),
            "uncommitted": outstanding,
        }
    args = ["restore", "--source", sha, "--"] + (paths or ["."])
    try:
        git(repo_path, *args)
    except subprocess.CalledProcessError as exc:
        return {"error": (exc.stderr or "").strip()[:240] or "restore failed"}
    return {
        "restored": paths or ["everything"],
        "from": sha,
        "changed": dirty(repo_path),
        "note": ("Written to your working tree and left uncommitted, so you "
                 "can read the diff before deciding. `git checkout -- .` "
                 "undoes it."),
    }
