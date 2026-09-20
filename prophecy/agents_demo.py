"""Three agents, three processes, one project.

Each one speaks to Prophecy the way a real coding agent would — its own MCP
process over stdio — so what this shows is genuinely concurrent rather than
three function calls in a loop. They coordinate through the project's
database, which is the only thing they share.
"""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

SCRIPT = [
    {"agent": "ada", "task": "Make email required during signup",
     "finding": ("app/models.py",
                 "create_user has five importers and every one omits email "
                 "today, so the default cannot simply be dropped"),
     "analyze": "agent-a/require-email"},
    {"agent": "grace", "task": "Redesign the signup form",
     "finding": ("api/signup.py",
                 "signup() builds the user directly instead of going through "
                 "login(), so changes here bypass the auth path"),
     "analyze": "agent-b/signup-redesign"},
    {"agent": "linus", "task": "Tidy up the user table",
     "finding": None, "analyze": "agent-c/user-cleanup"},
]


def call(repo, requests, timeout=180):
    """One MCP process, one agent, a handful of tool calls."""
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    out = subprocess.run(
        [sys.executable, "-m", "prophecy.mcp", str(repo)],
        input=payload, capture_output=True, text=True, timeout=timeout,
    )
    replies = []
    for line in out.stdout.splitlines():
        try:
            replies.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return replies


def interesting(text):
    """The part of a briefing that differs between agents.

    The repository half is identical for everyone — that is the entire point
    of it — so printing it three times hides the thing worth watching.
    """
    marker = "\n# Task:"
    if marker in text:
        head, rest = text.split(marker, 1)
        prefix_lines = len(head.strip().splitlines())
        return (f"[{prefix_lines} lines of shared repository background, "
                f"identical for every agent]\n# Task:" + rest)
    return text


def text_of(reply):
    if not reply or "result" not in reply:
        return f"(error: {(reply or {}).get('error', {}).get('message', '?')})"
    return reply["result"]["content"][0]["text"]


def tool(n, name, args):
    return {"jsonrpc": "2.0", "id": n, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


def act(repo, part, stage):
    """What one agent does at one stage, in its own process."""
    agent = part["agent"]
    if stage == "join":
        return agent, interesting(text_of(call(repo, [
            tool(1, "join_repo_session",
                 {"agent": agent, "task": part["task"]})])[0]))
    if stage == "share" and part["finding"]:
        path, finding = part["finding"]
        return agent, text_of(call(repo, [
            tool(1, "share_finding",
                 {"agent": agent, "file": path, "finding": finding})])[0])
    if stage == "analyze":
        return agent, text_of(call(repo, [
            tool(1, "analyze_change",
                 {"agent": agent, "head": part["analyze"],
                  "intent": part["task"]})])[0])
    if stage == "overlap":
        return agent, text_of(call(repo, [
            tool(1, "check_overlap", {"agent": agent})])[0])
    return agent, ""


def run(repo, on_step=print):
    """Play the whole thing, printing what each agent sees as it happens."""
    def stage(title, name, subset=SCRIPT):
        on_step(f"\n── {title} " + "─" * max(0, 58 - len(title)))
        with ThreadPoolExecutor(max_workers=len(subset)) as pool:
            for agent, said in pool.map(
                    lambda part: act(repo, part, name), subset):
                if not said:
                    continue
                on_step(f"\n  [{agent}]")
                for line in said.splitlines():
                    on_step(f"    {line}")

    stage("all three join at once, each in its own process", "join")
    stage("two of them write down what they worked out", "share",
          [p for p in SCRIPT if p["finding"]])
    stage("linus joins afterwards and is handed both findings", "join",
          [SCRIPT[2]])
    stage("each asks what its change would break", "analyze")
    stage("and who else is in the same code", "overlap", SCRIPT[:1])
    on_step("\nNothing above was coordinated by hand. Each agent ran on its "
            "own\nand saw the others through the project.")
