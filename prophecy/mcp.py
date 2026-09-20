"""An MCP server, so a teammate's agent can reach this mid-session.

The point of putting prophecy behind MCP rather than a CLI is timing. A CLI
gives an agent context when it starts. MCP lets it ask again at any moment —
which matters because the thing worth knowing ("someone else just started
editing the file you are in") arrives after you began.

MCP is JSON-RPC 2.0. Locally it is one JSON object per line on stdio; on
the network it is the same objects POSTed to `/mcp`. The handshake and the
methods that matter are about a hundred lines, so this implements them
directly rather than taking a dependency for a tool whose pitch is that it
has none.
"""

import hashlib
import json
import os
import sys

from . import store
from .agent import brief, stable_prefix, volatile_suffix
from .predict import predict
from .risk_engine import (analyze_change, interactions,
                          repository_risk)
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
                    "tool": {"type": "string", "description": (
                        "Which coding agent this is: Claude Code, Cursor, "
                        "Codex, and so on. Optional: taken from the MCP "
                        "handshake when not given."
                    )},
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
            "name": "analyze_change",
            "description": (
                "Before you commit: what could this change break, how badly, "
                "and why. Reads the dependency graph, the signature-level "
                "diff, git history and everything else in flight, and returns "
                "a risk range with the evidence behind it. Ask this instead of "
                "guessing whether a change is safe."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "head": {"type": "string",
                             "description": "Branch or commit to analyze. "
                                            "Defaults to the working branch."},
                    "base": {"type": "string",
                             "description": "What to compare against. "
                                            "Defaults to main."},
                    "agent": {"type": "string"},
                    "intent": {"type": "string",
                               "description": "What you are trying to do, in "
                                              "your own words. Used to check "
                                              "the diff against the intent."},
                },
            },
        },
        {
            "name": "get_repository_risk",
            "description": (
                "Risk across everything in flight: every branch and pull "
                "request, scored, plus where they meet. Use it to see whether "
                "now is a bad moment to touch a shared contract."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_change_interactions",
            "description": (
                "Pairs of changes that are calm on their own and dangerous "
                "together: one side changing what is stored while another "
                "changes the code that reads it, for example."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_dependency_context",
            "description": (
                "What reaches a file or symbol: direct importers, indirect "
                "ones, how critical it looks and why. Ask before modifying "
                "something you did not write."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
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


# What a coding agent calls itself in the MCP handshake, and what a person
# calls it. The handshake name is the honest source — an agent says who it is
# before it says anything else — but nobody reads "claude-code" as a product.
CLIENTS = {
    "claude-code": "Claude Code",
    "claude-ai": "Claude",
    "claude-desktop": "Claude Desktop",
    "cursor": "Cursor",
    "cursor-vscode": "Cursor",
    "windsurf": "Windsurf",
    "cline": "Cline",
    "continue": "Continue",
    "codex": "Codex",
    "codex-cli": "Codex CLI",
    "chatgpt": "ChatGPT",
    "openai": "ChatGPT",
    "copilot": "Copilot",
    "github-copilot": "Copilot",
    "zed": "Zed",
    "aider": "Aider",
    "gemini-cli": "Gemini CLI",
}


def client_name(raw):
    """A coding agent's product name, from whatever it called itself.

    Unknown clients keep their own name rather than being forced into this
    list — a tool Prophecy has never heard of is still the tool somebody is
    using, and guessing would be worse than repeating what it said.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    key = raw.lower().replace(" ", "-").replace("_", "-")
    if key in CLIENTS:
        return CLIENTS[key]
    for known, pretty in CLIENTS.items():
        if known in key:
            return pretty
    return raw


class Server:
    def __init__(self, repo_path):
        self.repo_path = os.path.abspath(repo_path)
        self.sessions = {}
        # filled in by the initialize handshake, before any tool is called
        self.client = ""

    def session_id(self, agent):
        return store.session_id(self.repo_path, agent)

    def _open(self):
        repo = scan(self.repo_path)
        return repo, store.connect(self.repo_path)

    def join(self, args):
        agent, task = args["agent"], args["task"]
        repo, db = self._open()
        session_id = self.session_id(agent)
        # which coding agent this is, as it introduced itself in the handshake;
        # an agent may also say so outright, and being told beats inferring
        store.join_session(db, session_id, repo["sha"], agent, task,
                           client_name(args.get("tool")) or self.client or "mcp")

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
                f"- {o['agent']}: {o['task']} ({o['kind'].replace('_', ' ')})"
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
            return ("Nothing else is in flight here right now: no other "
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

    def analyze(self, args):
        repo, db = self._open()
        base = args.get("base") or "main"
        head = args.get("head") or repo["branch"]
        agent = args.get("agent")
        concurrent = [w for w in in_flight(repo, base, db, store)
                      if w["agent"] != agent and head not in w["label"]]
        result = analyze_change(repo, base, head, label=head, agent=agent,
                                concurrent=concurrent,
                                stated_intent=args.get("intent", ""))
        if agent:
            store.log(db, repo["sha"], "analyzed", agent,
                      f"checked {head}: {result['risk_score']}/100 "
                      f"({result['risk_band']})")
        return _render_analysis(result)

    def repo_risk(self, args):
        repo, db = self._open()
        work = in_flight(repo, "main", db, store)
        analyses = [
            analyze_change(repo, "main", w["label"].split()[-1],
                           label=w["label"], agent=w["agent"],
                           concurrent=[o for o in work if o is not w])
            for w in work if w["kind"] == "branch"
        ]
        found = interactions(analyses)
        overall = repository_risk(analyses, found)
        lines = [f"Repository risk {overall['score']}/100 "
                 f"({overall['band']}), from {overall['changes']} change(s)."]
        lines += [f"- {d}" for d in overall["drivers"]]
        for a in sorted(analyses, key=lambda a: -a["risk_score"]):
            lines.append(f"\n{a['label']}: {a['risk_score']}/100 "
                         f"({a['risk_band']}), {a['agent']}")
            for f in a["potential_failures"][:2]:
                lines.append(f"  [{f['severity']}] {f['title']}")
        return "\n".join(lines)

    def change_interactions(self, args):
        repo, db = self._open()
        work = [w for w in in_flight(repo, "main", db, store)
                if w["kind"] == "branch"]
        analyses = [analyze_change(repo, "main", w["label"].split()[-1],
                                   label=w["label"], agent=w["agent"])
                    for w in work]
        found = interactions(analyses)
        if not found:
            return "Nothing in flight meets anything else right now."
        lines = []
        for i in found:
            lines.append(
                f"{' and '.join(i['between'])}: alone "
                f"{i['individual'][0]} and {i['individual'][1]}, together "
                f"{i['combined_score']} ({i['combined_band']})"
                + ("  <- this is worse than either on its own"
                   if i["escalates"] else "")
            )
            lines += [f"  - {e}" for e in i["evidence"]]
        return "\n".join(lines)

    def dependency_context(self, args):
        from .risk_engine import blast_radius, criticality
        repo, db = self._open()
        path = args["path"]
        if path not in repo["files"]:
            return f"{path} is not a code file in this repository."
        layers = blast_radius(repo, [path])
        crit, signals = criticality(repo, path, layers)
        info = repo["files"][path]
        lines = [
            f"{path}",
            f"  {len(info['symbols'])} symbol(s), "
            f"{len(repo['callers'].get(path, []))} direct importer(s)",
            f"  criticality {crit}/100",
        ]
        lines += [f"  - {s}" for s in signals]
        if layers:
            lines.append("  reached by: " + ", ".join(layers[0][:8]))
        return "\n".join(lines)

    def inbox(self, agent):
        """Anything a person addressed to this agent, newest last.

        Prepended to whatever the agent asked for, because an agent only reads
        when it is already reading: a message nobody fetches is a message
        nobody acts on.
        """
        if not agent:
            return ""
        _, db = self._open()
        waiting = store.pending_messages(db, agent)
        if not waiting:
            return ""
        blocks = [f"### {m['subject']}\n{m['body']}" for m in waiting]
        return ("## For you, from your team\n"
                + "\n\n".join(blocks)
                + "\n\n---\n\n")

    def call(self, name, args):
        handler = {
            "analyze_change": self.analyze,
            "get_repository_risk": self.repo_risk,
            "get_change_interactions": self.change_interactions,
            "get_dependency_context": self.dependency_context,
            "join_repo_session": self.join,
            "share_finding": self.share,
            "check_overlap": self.overlap,
            "leave_repo_session": self.leave,
        }.get(name)
        if not handler:
            raise ValueError(f"unknown tool {name}")
        answer = handler(args)
        # every tool an agent calls is a chance to hand it what is waiting
        if name != "leave_repo_session":
            return self.inbox(args.get("agent")) + answer
        return answer


def _render_analysis(a):
    """The same analysis a person sees, written for an agent to act on."""
    lines = [
        f"{a['label']}: risk {a['risk_score']}/100 ({a['risk_band']}), "
        f"range {a['risk_range']['min']}-{a['risk_range']['max']}, "
        f"confidence {int(a['confidence'] * 100)}%",
        f"Blast radius {a['blast_radius']['size']}: "
        f"{len(a['blast_radius']['direct'])} direct, "
        f"{len(a['blast_radius']['indirect'])} indirect.",
    ]
    if a["intent_contradictions"]:
        lines.append("\nThe message and the diff disagree:")
        lines += [f"- {c}" for c in a["intent_contradictions"]]
    if a["potential_failures"]:
        lines.append("\nWhat could break:")
        for f in a["potential_failures"]:
            lines.append(f"- [{f['severity']}] {f['title']}: {f['detail']}")
            if f["affected"]:
                lines.append(f"    {', '.join(f['affected'][:6])}")
    if a["concurrent_overlap"]:
        lines.append("\nHappening at the same time:")
        for o in a["concurrent_overlap"]:
            lines.append(f"- {o['agent']} on {o['with']}: "
                         f"{', '.join(o['shared_files'][:3])}")
    lines.append("\nNot known:")
    lines += [f"- {u}" for u in a["uncertainty"]]
    if a["recommendations"]:
        lines.append("\nSuggested:")
        lines += [f"- {r}" for r in a["recommendations"]]
    return "\n".join(lines)


# High and critical are the bands a person should not have to notice and click
# through. The agent still cannot be pushed — the profile waits for its next
# call — but nobody has to press Send.
WARN_AT = frozenset({"high", "critical"})


def should_warn(analysis, meets=None):
    if (analysis.get("risk_band") or "") in WARN_AT:
        return True
    return any((m.get("combined_band") or "") in WARN_AT for m in (meets or []))


def queue_risk_warning(db, repo, analysis, meets=None, sent_by="auto", force=False):
    """Put this profile in the attributed agent's inbox, if it is serious.

    `force` is the dashboard button: a person can send a medium profile by
    hand. Automatic warnings only fire for high and critical, including when
    two quieter changes become critical together.
    """
    agent = analysis.get("agent")
    if not agent:
        return None
    label = analysis.get("label") or ""
    mine = [m for m in (meets or []) if label in (m.get("between") or [])]
    if not force and not should_warn(analysis, mine):
        return None
    target = label.split()[-1]
    if not target:
        return None
    body = _render_analysis(analysis)
    if mine:
        body += "\n\nTogether with other work in flight:"
        for i in mine:
            others = [b for b in i["between"] if b != label]
            body += (f"\n- with {', '.join(others)}: {i['combined_score']}/100 "
                     f"({i['combined_band']}) combined, against "
                     f"{analysis['risk_score']} alone"
                     + (", worse together than apart" if i.get("escalates")
                        else ""))
            for line in (i.get("evidence") or [])[:3]:
                body += f"\n    {line}"
    subject = (f"Risk profile for {target}: {analysis['risk_score']}/100 "
               f"({analysis['risk_band']})")
    store.queue_message(db, repo["sha"], agent, subject, body, sent_by=sent_by)
    store.log(db, repo["sha"], "notified", agent,
              ("warned automatically" if sent_by == "auto"
               else "sent the risk profile")
              + f" for {target} ({analysis['risk_score']}/100)")
    return agent


def handle(server, request):
    """One JSON-RPC object in, a reply dict or None for a notification."""
    if not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid request"}}
    method, request_id = request.get("method"), request.get("id")
    try:
        if method == "initialize":
            info = (request.get("params") or {}).get("clientInfo") or {}
            server.client = client_name(info.get("name"))
            result = {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "prophecy", "version": "0.1.0"},
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
                return None  # a notification we do not handle
            raise ValueError(f"unknown method {method}")
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        if request_id is None:
            return None
        return {
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32000, "message": str(exc)[:300]},
        }


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
        reply = handle(server, request)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    return 0


def config_snippet(repo_path, url=None, token=""):
    """What to paste into an MCP client's config to reach this repo.

    A URL is the public shape: any agent that can POST JSON-RPC talks to
    this Prophecy. stdio is the fallback when nothing is listening on HTTP.
    """
    if url:
        prophecy = {"type": "http", "url": url}
        if token:
            prophecy["headers"] = {"Authorization": f"Bearer {token}"}
        return {"mcpServers": {"prophecy": prophecy}}
    return {
        "mcpServers": {
            "prophecy": {
                "command": sys.executable,
                "args": ["-m", "prophecy.mcp", os.path.abspath(repo_path)],
            }
        }
    }


if __name__ == "__main__":
    sys.exit(serve(sys.argv[1] if len(sys.argv) > 1 else "."))
