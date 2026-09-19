"""An MCP server, so a teammate's agent can reach this mid-session.

The point of putting mergemind behind MCP rather than a CLI is timing. A CLI
gives an agent context when it starts. MCP lets it ask again at any moment —
which matters because the thing worth knowing ("someone else just started
editing the file you are in") arrives after you began.

MCP is JSON-RPC 2.0 over stdio. The handshake and the three methods that
matter are about a hundred lines, so this implements them directly rather
than taking a dependency for a tool whose pitch is that it has none.
"""

import hashlib
import json
import os
import sys

from . import store
from .agent import brief, stable_prefix, volatile_suffix
from .predict import predict
from .work import in_flight
from .risk import risks
from .scan import scan

PROTOCOL = "2025-06-18"


def tools():
    return [
        {
            "name": "join_repo_session",
            "description": (
                "Register this agent as working in the repository and get a "
                "briefing: the files the task is likely to touch, the symbols "
                "in them, anything other agents already found there, and who "
                "else is working in the same code right now. Call this once "
                "before starting work."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string",
                              "description": "A name for this agent or developer."},
                    "task": {"type": "string",
                             "description": "What this session is about to work on."},
                },
                "required": ["agent", "task"],
            },
        },
        {
            "name": "share_finding",
            "description": (
                "Record something learned about a file so the next agent sent "
                "there is told. Use it for things that were expensive to work "
                "out and are not obvious from reading the file once."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "file": {"type": "string", "description": "Repo-relative path."},
                    "finding": {"type": "string"},
                },
                "required": ["agent", "file", "finding"],
            },
        },
        {
            "name": "check_overlap",
            "description": (
                "Ask who else is working in the files this agent is touching, "
                "and what the risk is. Worth calling again before editing a "
                "shared file: other sessions start after yours did."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "files": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Files this agent is about to change.",
                    },
                },
                "required": ["agent"],
            },
        },
        {
            "name": "leave_repo_session",
            "description": "Say this agent has finished, so it stops showing as live.",
            "inputSchema": {
                "type": "object",
                "properties": {"agent": {"type": "string"}},
                "required": ["agent"],
            },
        },
    ]


class Server:
    def __init__(self, repo_path):
        self.repo_path = os.path.abspath(repo_path)
        self.sessions = {}

    def session_id(self, agent):
        """Stable for an agent in a repo, not per process.

        Each MCP call may arrive in a fresh process. A random id per process
        meant one agent reconnecting showed up as a crowd.
        """
        seed = f"{self.repo_path}|{agent}".encode()
        return hashlib.blake2s(seed, digest_size=8).hexdigest()

    def _open(self):
        repo = scan(self.repo_path)
        return repo, store.connect(self.repo_path)

    def join(self, args):
        agent, task = args["agent"], args["task"]
        repo, db = self._open()
        session_id = self.session_id(agent)
        store.join_session(db, session_id, repo["sha"], agent, task, "mcp")

        forecast = predict(repo, task)
        # Everything else in flight, not just other live sessions: a teammate's
        # open pull request collides just as hard as a running agent.
        elsewhere = [w for w in in_flight(repo, "main", db, store)
                     if w["agent"] != agent]
        found = risks(repo, [forecast, *(w["forecast"] for w in elsewhere)])
        others = [
            {"agent": w["agent"], "task": w["label"], "kind": w["kind"]}
            for w in elsewhere
        ]
        data = brief(repo, [forecast], found, db=db, agent=agent, store=store)

        mine = [r for r in found if task in r["tasks"]]
        store.log(db, repo["sha"], "joined", agent,
                  f"joined, working on {task}", data["suffixes"][0]["tokens"])
        if mine:
            store.log(
                db, repo["sha"], "overlap", agent,
                f"heading for the same code as "
                f"{len(mine)} other piece{'s' if len(mine) != 1 else ''} of work",
            )

        text = data["prefix"] + "\n\n" + data["suffixes"][0]["text"]
        if others:
            text += "\n\n## Also in flight right now\n" + "\n".join(
                f"- {o['agent']} — {o['task']} ({o['kind'].replace('_', ' ')})"
                for o in others
            )
        return text

    def share(self, args):
        agent, path, finding = args["agent"], args["file"], args["finding"]
        repo, db = self._open()
        if path not in repo["files"]:
            return f"{path} is not a code file in this repository; nothing recorded."
        store.add_note(db, repo["sha"], agent, path, finding)
        store.log(db, repo["sha"], "shared", agent,
                  f"shared something about {path}: {finding}")
        return (f"Recorded against {path}. The next agent sent there gets it "
                "in their briefing.")

    def overlap(self, args):
        agent = args["agent"]
        repo, db = self._open()
        files = args.get("files") or []
        elsewhere = [w for w in in_flight(repo, "main", db, store)
                     if w["agent"] != agent]
        if not elsewhere:
            return ("Nothing else is in flight here right now — no other "
                    "sessions, branches or open pull requests.")

        mine = predict(repo, next(
            (s["task"] for s in store.live_sessions(db) if s["agent"] == agent),
            " ".join(files) or "",
        ))
        found = risks(repo, [mine, *(w["forecast"] for w in elsewhere)])
        live = [{"agent": w["agent"], "task": w["label"]} for w in elsewhere]
        store.touch_session(db, self.session_id(agent))

        if not found:
            return ("Others are working here, but not in the same files:\n"
                    + "\n".join(f"- {s['agent']}: {s['task']}" for s in live))
        lines = ["Overlapping work right now:"]
        for r in found[:6]:
            lines.append(
                f"- {r['risk_level']} {r['risk_type']} between "
                f"{' and '.join(r['tasks'])}: {r['recommendation']}"
            )
        return "\n".join(lines)

    def leave(self, args):
        agent = args["agent"]
        repo, db = self._open()
        store.leave_session(db, self.session_id(agent))
        store.log(db, repo["sha"], "left", agent, "finished up")
        return f"{agent} marked finished."

    def call(self, name, args):
        handler = {
            "join_repo_session": self.join,
            "share_finding": self.share,
            "check_overlap": self.overlap,
            "leave_repo_session": self.leave,
        }.get(name)
        if not handler:
            raise ValueError(f"unknown tool {name}")
        return handler(args)


def serve(repo_path=".", stdin=None, stdout=None):
    """Read JSON-RPC lines, write JSON-RPC lines. Notifications get no reply."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = Server(repo_path)

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        method, request_id = request.get("method"), request.get("id")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mergemind", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": tools()}
            elif method == "tools/call":
                params = request.get("params") or {}
                text = server.call(params.get("name"), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}]}
            elif method == "ping":
                result = {}
            else:
                if request_id is None:
                    continue  # a notification we do not handle
                raise ValueError(f"unknown method {method}")
            reply = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            if request_id is None:
                continue
            reply = {
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32000, "message": str(exc)[:300]},
            }

        if request_id is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    return 0


def config_snippet(repo_path):
    """What to paste into an MCP client's config to reach this repo."""
    return {
        "mcpServers": {
            "mergemind": {
                "command": sys.executable,
                "args": ["-m", "mergemind.mcp", os.path.abspath(repo_path)],
            }
        }
    }


if __name__ == "__main__":
    sys.exit(serve(sys.argv[1] if len(sys.argv) > 1 else "."))
