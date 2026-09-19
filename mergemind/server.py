"""A dashboard, served by the standard library.

The same functions the CLI calls, behind a few JSON endpoints, plus one
static HTML file. No build step, no node_modules, nothing to install.
"""

import json
import subprocess
import traceback
from argparse import Namespace
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import store
from .scan import scan

PAGE = Path(__file__).parent / "dashboard.html"


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, repo=".", **kwargs):
        self.repo_path = repo
        super().__init__(*args, **kwargs)

    def log_message(self, *args):
        pass  # the dashboard polls; the default log is just noise

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, "text/html", PAGE.read_bytes())
        if not url.path.startswith("/api/"):
            return self._send(404, "text/plain", b"not found")

        query = {k: v for k, v in parse_qs(url.query).items()}
        try:
            payload = self._call(url.path[5:], query)
        except Exception:
            traceback.print_exc()
            return self._send(500, "application/json",
                              json.dumps({"error": "see server log"}).encode())
        return self._send(200, "application/json",
                          json.dumps(payload, default=str).encode())

    def _call(self, name, query):
        from .cli import COMMANDS
        # The dashboard can point at any repo on this machine. The server is
        # bound to localhost and acts as the person running it, so the check
        # here is for a useful error message, not for isolation.
        path = (query.get("repo", [""])[0] or self.repo_path).strip()
        problem = _not_a_repo(path)
        if problem:
            return {"error": problem}
        repo = scan(path)
        if name == "file":
            return _file_detail(repo, query.get("path", [""])[0])
        if name == "repos":
            return {"repos": _discover_repos()}
        if name == "mcp_config":
            from .mcp import config_snippet
            return {"config": config_snippet(path), "repo": path}
        if name not in COMMANDS:
            return {"error": f"unknown command {name}"}
        db = store.connect(path)
        store.save_snapshot(db, repo)
        args = Namespace(
            json=True, repo=path,
            base=query.get("base", ["main"])[0],
            task=query.get("task", [""])[0],
            tasks=query.get("task", []),
            against=query.get("against", []),
            branch=query.get("branch", []),
            risk_id=query.get("risk_id", [""])[0],
            test=None, exact=False, request=False,
            llm=query.get("llm", ["0"])[0] in ("1", "true"),
            provider=query.get("provider", [None])[0],
            numbers=[int(n) for n in query.get("number", []) if n.isdigit()],
            comment=None,
            agent=query.get("agent", [None])[0],
            file=query.get("file", [""])[0],
            text=query.get("text", [""])[0],
            confirm=query.get("confirm", ["0"])[0] in ("1", "true"),
            config=False,
            limit=int(query.get("limit", ["25"])[0]),
            ref=query.get("ref", ["HEAD"])[0],
        )
        if name == "verify":
            args.branch = query.get("branch", [""])[0]
        result = COMMANDS[name](repo, args, db)
        if name in ("scan", "status"):
            result["graph"] = _graph(repo)
        return result

    def _send(self, code, kind, body):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


SKIP = {"node_modules", "venv", ".venv", "vendor", "Library", "Applications"}


def _discover_repos(limit=60):
    """Git repositories under the places people actually keep them.

    Depth-limited on purpose: walking a whole home directory to populate a
    dropdown is a good way to make the page feel broken.
    """
    home = Path.home()
    roots = [home, *(home / n for n in (
        "Projects", "projects", "code", "Code", "src", "dev", "Developer",
        "repos", "work", "Documents", "Desktop", "git",
    ))]
    found, seen = [], set()

    def looks_like_repo(d):
        return (d / ".git").exists()

    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except PermissionError:
            continue
        for entry in entries:
            if len(found) >= limit:
                break
            if not entry.is_dir() or entry.name.startswith(".") \
                    or entry.name in SKIP:
                continue
            candidates = [entry]
            if not looks_like_repo(entry):
                try:  # one level further down, for ~/code/org/repo layouts
                    candidates = [c for c in sorted(entry.iterdir())
                                  if c.is_dir() and not c.name.startswith(".")][:40]
                except PermissionError:
                    continue
            for candidate in candidates:
                key = str(candidate.resolve())
                if key in seen or not looks_like_repo(candidate):
                    continue
                seen.add(key)
                found.append({"name": candidate.name, "path": key})
    return sorted(found, key=lambda r: r["name"].lower())


def _not_a_repo(path):
    """A sentence the person can act on, or None if the path is fine."""
    directory = Path(path).expanduser()
    if not directory.exists():
        return f"No such directory: {directory}"
    if not directory.is_dir():
        return f"{directory} is a file, not a directory"
    inside = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--git-dir"],
        capture_output=True, text=True,
    )
    if inside.returncode:
        return f"{directory} is not a git repository. Run git init, or pick another folder."
    if subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"],
                      capture_output=True).returncode:
        return f"{directory} is a git repository with no commits yet."
    return None


def _file_detail(repo, path):
    """Everything known about one file, for the detail drawer."""
    from .scan import is_regenerated
    info = repo["files"].get(path)
    if not info:
        return {"error": f"{path} is not a code file in this repo"}
    return {
        "path": path,
        "lines": info["lines"],
        "symbols": info["symbols"],
        "imports": info["imports"],
        "callers": repo["callers"].get(path, []),
        "is_test": info["is_test"],
        "is_schema": path in repo["schema_files"],
        "is_regenerated": is_regenerated(path),
    }


def _graph(repo):
    """Nodes and edges for the dependency picture. Files only — a task graph
    that redraws on every keystroke is not worth the wire."""
    edges = [
        {"from": caller, "to": target}
        for target, callers in repo["callers"].items() for caller in callers
    ]
    degree = {}
    for edge in edges:
        degree[edge["to"]] = degree.get(edge["to"], 0) + 1
    nodes = [
        {
            "id": rel,
            "depended_on_by": degree.get(rel, 0),
            "symbols": len(info["symbols"]),
            "is_test": info["is_test"],
            "is_schema": rel in repo["schema_files"],
        }
        for rel, info in repo["files"].items()
    ]
    nodes.sort(key=lambda n: -n["depended_on_by"])
    return {"nodes": nodes, "edges": edges}


def serve(repo_path, port):
    server = HTTPServer(("127.0.0.1", port), partial(Handler, repo=repo_path))
    print(f"mergemind dashboard on http://127.0.0.1:{port}  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0
