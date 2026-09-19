"""A dashboard, served by the standard library.

The same functions the CLI calls, behind a few JSON endpoints, plus one
static HTML file. No build step, no node_modules, nothing to install.
"""

import json
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
        if name not in COMMANDS:
            return {"error": f"unknown command {name}"}
        repo = scan(self.repo_path)
        db = store.connect(self.repo_path)
        store.save_snapshot(db, repo)
        args = Namespace(
            json=True, repo=self.repo_path,
            base=query.get("base", ["main"])[0],
            task=query.get("task", [""])[0],
            tasks=query.get("task", []),
            against=query.get("against", []),
            branch=query.get("branch", []),
            risk_id=query.get("risk_id", [""])[0],
            test=None,
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
    return {"nodes": nodes, "edges": edges}


def serve(repo_path, port):
    server = HTTPServer(("127.0.0.1", port), partial(Handler, repo=repo_path))
    print(f"mergemind dashboard on http://127.0.0.1:{port}  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0
