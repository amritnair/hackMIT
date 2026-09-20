"""Reading, editing and committing one file, from inside the map.

Prophecy spends its time telling you what a change to a file would break. The
honest next step, once you are looking at the file it is worried about, is to
change the file — so this is the smallest amount of git that makes that real:
read it, write it, see what actually differs, commit it.

Every path here is checked against the tracked code files the scan already
found, rather than against string prefixes. A path that is not something
Prophecy parsed is not something this will write to.
"""

import subprocess
from pathlib import Path

from .scan import git


def _run(repo_path, *args):
    """git, but handing back failure instead of raising.

    Committing fails for ordinary reasons — nothing staged, no name configured
    — and those are answers to show someone, not stack traces.

    Only the trailing newline is removed. `status --porcelain` says "unstaged"
    with a leading space, so stripping both ends would turn every ordinary edit
    into a staged one and eat the first letter of its path.
    """
    done = subprocess.run(["git", "-C", str(repo_path), *args],
                          capture_output=True, text=True)
    return done.returncode, done.stdout.rstrip("\n"), done.stderr.strip()


def _resolve(repo, rel):
    """The real path of a tracked code file, or None if it is not one."""
    if rel not in repo["files"]:
        return None
    root = Path(repo["repo"]).resolve()
    path = (root / rel).resolve()
    # a tracked name cannot escape the repo, but the check is cheap and the
    # consequence of being wrong is writing to somebody's home directory
    if root not in path.parents and path != root:
        return None
    return path


def read_file(repo, rel):
    path = _resolve(repo, rel)
    if not path:
        return {"error": f"{rel} is not a code file in this repo"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"error": f"cannot read {rel}: {exc.strerror or exc}"}
    return {
        "path": rel,
        "content": text,
        "lines": text.count("\n") + 1,
        "language": path.suffix.lstrip("."),
    }


def write_file(repo, rel, content):
    """Save the editor's text over the file.

    Newline at the end of file, because every tool downstream of this assumes
    one and a diff full of "\\ No newline at end of file" is noise nobody asked
    for.
    """
    path = _resolve(repo, rel)
    if not path:
        return {"error": f"{rel} is not a code file in this repo"}
    if content and not content.endswith("\n"):
        content += "\n"
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"error": f"cannot write {rel}: {exc.strerror or exc}"}
    return {"saved": rel, "lines": content.count("\n") + 1,
            "worktree": worktree(repo["repo"])}


# porcelain's two columns are the index and the working tree, in that order
STATE = {
    "M": "modified", "A": "added", "D": "deleted", "R": "renamed",
    "C": "copied", "U": "conflicted", "?": "untracked", "!": "ignored",
}


def worktree(repo_path):
    """What differs from the last commit, split into staged and not.

    This is the source-control panel: the same answer `git status` gives, in
    the shape a list renders from.
    """
    code, out, _ = _run(repo_path, "status", "--porcelain")
    if code != 0:
        return {"files": [], "branch": "", "clean": True}
    files = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        index, work, rest = line[0], line[1], line[3:]
        # renames read "old -> new"; the new name is the one to show
        path = rest.split(" -> ")[-1].strip().strip('"')
        # prophecy's own database is not the user's change to review
        if path.startswith(".prophecy/") or path == ".prophecy":
            continue
        files.append({
            "path": path,
            "staged": index not in (" ", "?"),
            "unstaged": work != " ",
            "state": STATE.get(index if index != " " else work, "changed"),
            "untracked": index == "?",
        })
    branch, head = "", ""
    code, out, _ = _run(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    if code == 0:
        branch = out.strip()
    code, out, _ = _run(repo_path, "log", "-1", "--format=%h %s")
    if code == 0:
        head = out.strip()
    return {"files": files, "branch": branch, "head": head,
            "clean": not files}


def diff_file(repo_path, rel):
    """The unified diff for one file, staged changes included."""
    code, out, _ = _run(repo_path, "diff", "HEAD", "--unified=3", "--", rel)
    if code != 0 or not out:
        # a file git has never seen has no diff; every line of it is new
        code, out, _ = _run(repo_path, "diff", "--no-index", "--unified=3",
                            "/dev/null", str(Path(repo_path) / rel))
    return out


def commit(repo_path, paths, message):
    """Stage these paths and commit them, for real.

    Scoped to the paths asked for. Someone editing one file in the map has not
    agreed to commit everything else that happens to be dirty in their tree.
    """
    message = (message or "").strip()
    if not message:
        return {"error": "a commit needs a message"}
    if not paths:
        return {"error": "nothing selected to commit"}

    code, _, err = _run(repo_path, "add", "--", *paths)
    if code != 0:
        return {"error": err[:200] or "could not stage that"}

    staged = _run(repo_path, "diff", "--cached", "--name-only")[1]
    if not staged:
        return {"error": "those files match the last commit already"}

    code, out, err = _run(repo_path, "commit", "-m", message, "--", *paths)
    if code != 0:
        detail = (err or out or "").strip()
        if "please tell me who you are" in detail.lower():
            detail = ("git does not know who you are yet. Set user.name and "
                      "user.email, then commit again")
        return {"error": detail[:300] or "git refused the commit"}

    sha = _run(repo_path, "rev-parse", "--short", "HEAD")[1]
    return {
        "committed": sha,
        "message": message,
        "files": staged.splitlines(),
        "worktree": worktree(repo_path),
    }


def revert_file(repo_path, rel):
    """Throw away uncommitted edits to one file."""
    code, _, err = _run(repo_path, "checkout", "HEAD", "--", rel)
    if code != 0:
        return {"error": err[:200] or f"could not restore {rel}"}
    return {"reverted": rel, "worktree": worktree(repo_path)}
