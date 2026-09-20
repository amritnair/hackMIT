"""A dashboard, served by the standard library.

The same functions the CLI calls, behind a few JSON endpoints, plus one
static HTML file. No build step, no node_modules, nothing to install.
"""

import json
import os
import socket
import subprocess
import traceback
import uuid
from argparse import Namespace
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import store
from .scan import scan

PAGE = Path(__file__).parent / "dashboard.html"
LOGO = Path(__file__).parent / "logo.png"
DEMO = Path(__file__).parent / "mcp-demo.html"


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
        if url.path == "/logo.png":
            return self._send(200, "image/png", LOGO.read_bytes())
        if url.path == "/mcp-demo.html":
            return self._send(200, "text/html", DEMO.read_bytes())
        if url.path == "/mcp":
            return self._mcp_get()
        if not url.path.startswith("/api/"):
            return self._send(404, "text/plain", b"not found")

        query = {k: v for k, v in parse_qs(url.query).items()}
        try:
            payload = self._call(url.path[5:], query)
        except Exception:
            traceback.print_exc()
            # the page never shows this; it is for whoever is running it
            return self._send(500, "application/json", json.dumps({
                "error": "internal",
                "detail": "prophecy hit an error handling that request; "
                          "the traceback is in the terminal running it",
            }).encode())
        return self._send(200, "application/json",
                          json.dumps(payload, default=str).encode())

    def do_OPTIONS(self):
        if urlparse(self.path).path == "/mcp":
            return self._send(204, "text/plain", b"", cors=True)
        return self._send(404, "text/plain", b"not found")

    def do_POST(self):
        """MCP is JSON-RPC in the body; file edits are too big for a query string."""
        url = urlparse(self.path)
        if url.path == "/mcp":
            return self._mcp_post()
        if not url.path.startswith("/api/"):
            return self._send(404, "text/plain", b"not found")
        try:
            size = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, "application/json",
                              json.dumps({"error": "unreadable request"}).encode())
        try:
            payload = self._write(url.path[5:], body)
        except Exception:
            traceback.print_exc()
            return self._send(500, "application/json", json.dumps({
                "error": "internal",
                "detail": "prophecy hit an error handling that request; "
                          "the traceback is in the terminal running it",
            }).encode())
        return self._send(200, "application/json",
                          json.dumps(payload, default=str).encode())

    def _write(self, name, body):
        """The endpoints that change the repository rather than read it."""
        from . import workspace
        path = (body.get("repo") or self.repo_path or "").strip()
        problem = _not_a_repo(path)
        if problem:
            return {"error": problem}
        if name == "save":
            return workspace.write_file(scan(path), body.get("path", ""),
                                        body.get("content", ""))
        if name == "commit":
            return workspace.commit(path, body.get("paths") or [],
                                    body.get("message", ""))
        if name == "revert":
            return workspace.revert_file(path, body.get("path", ""))
        return {"error": f"unknown command {name}"}

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
        if name == "setup":
            return _setup_state(path)
        if name == "new":
            from .create import new_project
            return new_project(
                query.get("path", [""])[0],
                query.get("name", [None])[0],
                query.get("github", [None])[0] or None,
                query.get("private", ["1"])[0] != "0",
            )
        if name == "sample":
            return _sample_project()
        if name == "repos":
            return {"repos": _discover_repos()}
        if name == "mcp_config":
            from .mcp import config_snippet
            token = os.environ.get("PROPHECY_MCP_TOKEN", "")
            url = self._public_mcp_url()
            return {"config": config_snippet(path, url=url, token=token),
                    "url": url, "repo": path}
        if name == "notify":
            return _notify_agent(
                path, repo,
                query.get("agent", [""])[0],
                query.get("target", [""])[0],
                query.get("base", ["main"])[0],
            )
        if name == "read":
            from . import workspace
            return workspace.read_file(repo, query.get("path", [""])[0])
        if name == "worktree":
            from . import workspace
            return workspace.worktree(path)
        if name == "filediff":
            from . import workspace
            return {"diff": workspace.diff_file(path, query.get("path", [""])[0])}
        if name == "sharing":
            return store.sharing(store.connect(path))
        if name == "messages":
            db = store.connect(path)
            return {
                "messages": store.messages(db),
                "live": [s["agent"] for s in store.live_sessions(db)],
            }
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
            action=query.get("action", ["list"])[0],
            name=query.get("name", [""])[0],
            github=query.get("github", [""])[0],
            role=query.get("role", [""])[0],
            target=query.get("target", ["HEAD"])[0],
            sha=query.get("sha", [""])[0],
            paths=[x for x in query.get("path", []) if x],
            force=query.get("force", ["0"])[0] in ("1", "true"),
            preview=query.get("preview", ["0"])[0] in ("1", "true"),
            max=int(query.get("max", ["70"])[0]),
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

    def _send(self, code, kind, body, cors=False, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS, DELETE")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, Accept, Authorization, "
                             "Mcp-Session-Id, MCP-Protocol-Version")
            self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _public_mcp_url(self):
        host = (self.headers.get("Host") or "127.0.0.1").strip()
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        return f"{proto}://{host}/mcp"

    def _mcp_headers(self):
        sid = getattr(self.server, "mcp_session", None)
        return {"Mcp-Session-Id": sid} if sid else {}

    def _mcp_server(self):
        from .mcp import Server
        held = getattr(self.server, "mcp", None)
        if held is None:
            self.server.mcp = Server(self.repo_path)
            held = self.server.mcp
        return held

    def _mcp_auth(self):
        token = os.environ.get("PROPHECY_MCP_TOKEN", "")
        if not token:
            return True
        got = self.headers.get("Authorization") or ""
        return got == f"Bearer {token}"

    def _mcp_get(self):
        """Browsers get a description. MCP clients that want SSE can POST."""
        from .mcp import PROTOCOL
        body = json.dumps({
            "name": "prophecy",
            "transport": "streamable-http",
            "protocolVersion": PROTOCOL,
            "url": self._public_mcp_url(),
        }).encode()
        return self._send(200, "application/json", body, cors=True,
                          headers=self._mcp_headers())

    def _mcp_post(self):
        from .mcp import handle
        if not self._mcp_auth():
            return self._send(401, "application/json", json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32001, "message": "unauthorized"},
            }).encode(), cors=True)
        try:
            size = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, "application/json", json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"},
            }).encode(), cors=True, headers=self._mcp_headers())
        batch = isinstance(payload, list)
        replies = []
        for req in (payload if batch else [payload]):
            reply = handle(self._mcp_server(), req)
            if reply is not None:
                replies.append(reply)
        if not replies:
            self.send_response(202)
            self.send_header("Access-Control-Allow-Origin", "*")
            for key, value in self._mcp_headers().items():
                self.send_header(key, value)
            self.end_headers()
            return
        body = json.dumps(replies if batch else replies[0]).encode()
        return self._send(200, "application/json", body, cors=True,
                          headers=self._mcp_headers())


SKIP = {"node_modules", "venv", ".venv", "vendor", "Library", "Applications"}


def _sample_project():
    """Build the demo project on demand, or hand back the one already there.

    Somewhere stable rather than a temp directory, so the link a person keeps
    open still works tomorrow.
    """
    from .demo import build, seed
    home = Path.home() / "prophecy-demo"
    if home.exists() and (home / ".git").exists():
        return {"path": str(home), "existed": True}
    built = build(home)
    if built.get("error"):
        return built
    try:
        seed(built["path"])
    except Exception:
        pass  # the project is still worth opening without its seeded history
    return {"path": built["path"], "existed": False,
            "branches": built.get("branches", [])}


def _setup_state(path):
    """How far through setup this project is, and what is left to do."""
    from . import github, store as store_mod
    problem = _not_a_repo(path)
    if problem:
        return {"repo_ok": False, "problem": problem, "github": None,
                "agents": 0, "people": 0}

    slug, remote_error = None, None
    try:
        slug = github.slug(path)
        if not slug:
            remote_error = "This project has no GitHub remote yet."
        else:
            github.pull_requests(path, limit=1)
    except github.GitHubUnavailable as exc:
        remote_error = str(exc)

    db = store_mod.connect(path)
    sessions = db.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
    return {
        "repo_ok": True,
        "problem": None,
        # Slug from origin counts as linked even when `gh` auth is stale —
        # PR reads need auth, but the repo connection itself is the remote.
        "github": slug,
        "github_auth": bool(slug) and not remote_error,
        "github_problem": remote_error,
        "agents": sessions,
        "live": len(store_mod.live_sessions(db)),
        "people": len(store_mod.members(db)),
    }


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


def _notify_agent(path, repo, agent, target, base):
    """Queue the risk profile of one change for the agent doing that change.

    Deliberately runs the same command the dashboard's own list runs and picks
    the matching change out of it, rather than analysing the branch a second
    time here. Two code paths would be two chances to send an agent a number
    the person looking at the page never saw.
    """
    from .cli import COMMANDS
    from .mcp import _render_analysis

    if not agent:
        return {"error": "who is this for?"}
    if not target:
        return {"error": "which change?"}

    db = store.connect(path)
    args = Namespace(json=True, repo=path, base=base, tasks=[])
    found = COMMANDS["risk"](repo, args, db)
    changes = found.get("changes", [])
    analysis = next((c for c in changes if target in (c.get("label") or "")), None)
    if not analysis:
        return {"error": f"{target} is not in flight here"}

    body = _render_analysis(analysis, detail=True)

    # Two changes can edit different files and still collide through what they
    # reach. That is the part an agent cannot work out alone, so it is the part
    # worth sending: its own reading plus what it looks like next to everyone
    # else's.
    label = analysis["label"]
    meets = [i for i in found.get("interactions", []) if label in i["between"]]
    if meets:
        body += "\n\nTogether with other work in flight:"
        for i in meets:
            others = [b for b in i["between"] if b != label]
            body += (f"\n- with {', '.join(others)}: {i['combined_score']}/100 "
                     f"({i['combined_band']}) combined, against "
                     f"{analysis['risk_score']} alone"
                     + (", worse together than apart" if i.get("escalates")
                        else ""))
            for line in i.get("evidence", [])[:3]:
                body += f"\n    {line}"

    subject = (f"Risk profile for {target}: {analysis['risk_score']}/100 "
               f"({analysis['risk_band']})")
    _, created = store.enqueue_message(db, repo["sha"], agent, subject, body)
    if created:
        store.log(db, repo["sha"], "notified", agent,
                  f"sent the risk profile for {target} "
                  f"({analysis['risk_score']}/100)")
    live = any(s["agent"] == agent for s in store.live_sessions(db))
    return {
        "queued": True,
        "agent": agent,
        "subject": subject,
        "body": body,
        "live": live,
        "score": analysis["risk_score"],
        "band": analysis["risk_band"],
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


def _lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def serve(repo_path, port, host="0.0.0.0"):
    from .mcp import Server
    # Threaded, because browsers hold idle speculative connections open and a
    # single-threaded server would sit waiting on one instead of answering.
    httpd = ThreadingHTTPServer((host, port), partial(Handler, repo=repo_path))
    httpd.daemon_threads = True
    httpd.mcp = Server(repo_path)
    httpd.mcp_session = str(uuid.uuid4())
    lan = _lan_ip()
    print(f"prophecy dashboard  http://127.0.0.1:{port}")
    print(f"MCP for agents      http://{lan}:{port}/mcp")
    print(f"                    claude mcp add --transport http prophecy "
          f"http://{lan}:{port}/mcp")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0
