"""Start a new project that is already wired up.

The setup nobody does is the setup that happens after the repo exists. A repo
created here ships with the agent configuration committed, so the first agent
anyone points at it joins the project without being told to.
"""

import json
import subprocess
import sys
from pathlib import Path

README = """# {name}

## Working here with agents

This project is set up with mergemind. Any agent that opens it can see what
everyone else is working on, and will be told what other people have already
worked out about a file before it changes it.

Nothing to install for that to work: the settings are in `.mcp.json`.
"""

GITIGNORE = """__pycache__/
*.pyc
.venv/
node_modules/
.mergemind/
"""


def run(cwd, *args, check=True):
    return subprocess.run(args, cwd=str(cwd), capture_output=True,
                          text=True, check=check)


def new_project(path, name=None, github=None, private=True):
    """Create a git repository with the agent wiring already in it.

    `github` is opt-in and does something public: it creates a repository on
    someone's account. It only happens when a caller passes it explicitly.
    """
    root = Path(path).expanduser().resolve()
    name = name or root.name
    if root.exists() and any(root.iterdir()):
        return {"error": f"{root} already exists and is not empty."}
    root.mkdir(parents=True, exist_ok=True)

    (root / "README.md").write_text(README.format(name=name))
    (root / ".gitignore").write_text(GITIGNORE)
    (root / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "mergemind": {
                "command": sys.executable,
                "args": ["-m", "mergemind.mcp", str(root)],
            }
        }
    }, indent=2) + "\n")

    run(root, "git", "init", "-q")
    run(root, "git", "add", "-A")
    run(root, "git", "-c", "user.email=mergemind@local",
        "-c", "user.name=mergemind", "commit", "-qm",
        "Start project, wired for agent coordination")

    result = {"path": str(root), "name": name, "remote": None}
    if github:
        made = run(root, "gh", "repo", "create", github,
                   "--private" if private else "--public",
                   "--source", ".", "--push", check=False)
        if made.returncode:
            result["github_error"] = made.stderr.strip()[:300]
        else:
            result["remote"] = made.stdout.strip() or github
    return result
