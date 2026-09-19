"""Builds a small throwaway repo and checks the whole pipeline against it.

Run with: python test_mergemind.py
"""

import subprocess
import tempfile
from pathlib import Path

import mergemind
from mergemind import agent, backfill, branches, llm, mcp, merge, store

FIXTURE = {
    "api/middleware.py": (
        "def authenticate(request, token):\n"
        '    """Check the caller token."""\n'
        "    return bool(token)\n\n"
        "def rate_limit(request, limit=100):\n"
        "    return True\n"
    ),
    "api/handlers.py": (
        "from api.middleware import authenticate, rate_limit\n\n"
        "def handle_request(request):\n"
        "    return authenticate(request, request.token)\n"
    ),
    "billing/invoice.py": "def charge(customer, cents):\n    return cents\n",
    "migrations/0001_add_users.sql": "CREATE TABLE users (id INT);\n",
    "requirements.txt": "flask\n",
    "tests/test_middleware.py": (
        "from api.middleware import rate_limit\n\n"
        "def test_rate_limit():\n    assert rate_limit(None)\n"
    ),
}


def build_repo(root):
    for rel, body in FIXTURE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                    capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    run("add", "-A")
    run("commit", "-qm", "fixture")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_repo(root)
        repo = mergemind.scan(root)

        # scan finds symbols, signatures and who imports what
        assert len(repo["sha"]) == 40
        assert "api/middleware.py" in repo["files"]
        signatures = {s["signature"] for s in repo["files"]["api/middleware.py"]["symbols"]}
        assert "rate_limit(request, limit)" in signatures, signatures
        assert repo["callers"]["api/middleware.py"] == [
            "api/handlers.py", "tests/test_middleware.py",
        ], repo["callers"]
        assert repo["schema_files"] == ["migrations/0001_add_users.sql"]
        assert repo["files"]["tests/test_middleware.py"]["is_test"]

        # a task lands on the right file, with evidence and honest gaps
        rate = mergemind.predict(repo, "Add rate limiting to the API")
        assert rate["files"][0]["file"] == "api/middleware.py", rate["files"]
        assert any("rate_limit" in e for e in rate["files"][0]["evidence"])
        assert "sms" in mergemind.predict(repo, "Send SMS reminders")["unsupported_terms"]

        # same file, different functions -> flagged, but not as a contract break
        auth = mergemind.predict(repo, "Add authentication middleware")
        found = mergemind.risks(repo, [rate, auth])
        top = found[0]
        assert top["risk_type"] == "shared_file", top
        assert top["risk_level"] == "medium", top
        assert any("import" in e for e in top["evidence"]), top["evidence"]
        # ids are content hashes, so they survive a rerun and `explain` keeps working
        assert top["id"] == mergemind.risks(repo, [rate, auth])[0]["id"]
        assert top["id"].startswith("R") and len(top["id"]) == 7

        # same function in scope for both -> contract risk, and it outranks the above
        reauth = mergemind.predict(repo, "Refactor how requests authenticate")
        contract = mergemind.risks(repo, [auth, reauth])[0]
        assert contract["risk_type"] == "shared_api_contract", contract
        assert contract["risk_level"] == "high", contract
        assert "authenticate" in contract["recommendation"], contract
        assert contract["risk_score"] > top["risk_score"]

        billing = mergemind.predict(repo, "Charge the customer an invoice")
        assert not mergemind.risks(repo, [billing, rate])

        # strategies and capsule render without blowing up
        assert len(mergemind.strategies(repo, [rate, auth], found)) == 3
        text = mergemind.capsule(repo, rate, found)
        assert "api/middleware.py" in text and "Coordination" in text

        check_branches(root, repo)
        check_backfill(root)
        check_brief(repo)
        check_llm(repo)
        check_sharing(root, repo)
        check_mcp(root)

    print("ok")


def rpc(root, *calls):
    """Drive the MCP server the way a client does: JSON-RPC lines in and out."""
    import io, json as _json
    lines = "\n".join(_json.dumps(c) for c in calls) + "\n"
    out = io.StringIO()
    mcp.serve(root, stdin=io.StringIO(lines), stdout=out)
    return [_json.loads(l) for l in out.getvalue().splitlines()]


def check_mcp(root):
    """An agent joins, learns something, and the next agent is told — over the
    wire, not by calling the functions directly."""
    init, listed = rpc(root,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert init["result"]["serverInfo"]["name"] == "mergemind"
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names == {"join_repo_session", "share_finding", "check_overlap",
                     "leave_repo_session"}, names

    def call(tool, **args):
        reply = rpc(root, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})[0]
        assert "error" not in reply, reply
        return reply["result"]["content"][0]["text"]

    first = call("join_repo_session", agent="ada",
                 task="Add rate limiting to the API")
    assert "api/middleware.py" in first
    # the fixture already has branches, and a joining agent should hear about
    # them, not only about other live sessions
    assert "Also in flight" in first and "(branch)" in first, first[-400:]
    assert "ada" not in first.split("Also in flight")[1]  # not told about itself

    call("share_finding", agent="ada", file="api/middleware.py",
         finding="rate_limit is a stub that always returns True")

    second = call("join_repo_session", agent="grace",
                  task="Add rate limiting to the API")
    assert "always returns True" in second, "ada's finding did not reach grace"
    assert "Also in flight" in second
    tail = second.split("Also in flight")[1]
    assert "ada" in tail and "(session)" in tail  # the live agent, labelled
    assert "(branch)" in tail  # and the branches, still

    # a path the repo does not have is refused rather than recorded
    refused = call("share_finding", agent="ada", file="nope/nothing.py",
                   finding="...")
    assert "not a code file" in refused

    overlap = call("check_overlap", agent="grace")
    assert "ada" in overlap or "Overlapping" in overlap, overlap

    # reconnecting is the same session, not a second one
    db = store.connect(root)
    call("join_repo_session", agent="ada", task="Add rate limiting to the API")
    live = store.live_sessions(db)
    assert sorted(s["agent"] for s in live) == ["ada", "grace"], live

    call("leave_repo_session", agent="ada")
    assert [s["agent"] for s in store.live_sessions(db)] == ["grace"]

    # an unknown method answers with an error, not a crash
    bad = rpc(root, {"jsonrpc": "2.0", "id": 3, "method": "nonsense"})[0]
    assert bad["error"]["code"] == -32000


def check_sharing(root, repo):
    """What one agent works out, the next one is told — and the accounting
    only counts a prefix as reused when the bytes really did repeat."""
    db = store.connect(root)
    rate = mergemind.predict(repo, "Add rate limiting to the API")
    found = mergemind.risks(repo, [rate])

    first = agent.brief(repo, [rate], found, db=db, agent="agent-a", store=store)
    assert first["suffixes"][0]["notes_pulled"] == 0
    assert "other agents have already found" not in first["suffixes"][0]["text"]

    store.add_note(db, repo["sha"], "agent-a", "api/middleware.py",
                   "rate_limit returns True unconditionally; it is a stub")

    second = agent.brief(repo, [rate], found, db=db, agent="agent-b", store=store)
    assert second["suffixes"][0]["notes_pulled"] == 1
    assert "it is a stub" in second["suffixes"][0]["text"]
    assert "agent-a" in second["suffixes"][0]["text"]

    # an agent is never handed its own note back
    again = agent.brief(repo, [rate], found, db=db, agent="agent-a", store=store)
    assert again["suffixes"][0]["notes_pulled"] == 0, "agent-a got its own note"

    # the note rides in the volatile half; the cached half must not move
    assert first["prefix_hash"] == second["prefix_hash"] == again["prefix_hash"]
    assert first["prefix"] == second["prefix"]

    data = store.usage(db)
    assert data["briefs"] == 3 and data["agents"] == 2
    assert data["prefix_first_time"] == 1, data
    assert data["prefix_reused"] == 2, data
    assert data["tokens_avoided"] > 0
    assert data["notes_written"] == 1 and data["notes_pulled"] == 1
    assert "not confirmation that it did" in data["note"]


class StubProvider:
    """Answers with one real file and one that does not exist."""
    name, model = "stub", "stub-1"

    def __init__(self, payload):
        self.payload = payload
        self.seen = None

    def complete(self, stable, volatile, max_tokens=1500):
        self.seen = (stable, volatile)
        return self.payload


def check_llm(repo):
    """A model may not name a file into existence."""
    provider = StubProvider(
        'Sure! ```json\n{"files":['
        '{"path":"api/middleware.py","why":"throttling lives here","confidence":0.9},'
        '{"path":"api/ratelimit/redis_backend.py","why":"invented","confidence":0.8}'
        ']}\n```'
    )
    out = llm.semantic_predict(repo, "throttle incoming requests", provider)
    assert [f["file"] for f in out["files"]] == ["api/middleware.py"], out
    assert out["invented"] == ["api/ratelimit/redis_backend.py"], out
    assert "stub" in out["files"][0]["evidence"][0]
    assert out["files"][0]["source"] == "model"

    # the model was shown the real inventory, and the task went after it
    stable, volatile = provider.seen
    assert "api/middleware.py" in stable and "rate_limit" in stable
    assert "throttle" in volatile and "throttle" not in stable
    assert stable == llm.inventory(repo)  # byte-stable, so it stays cached

    # merging keeps repository evidence ahead of model inference
    lexical = mergemind.predict(repo, "Charge the customer an invoice")
    merged = llm.merge_forecasts(lexical, out)
    assert merged["files"][0]["source"] == "repo", merged["files"]
    assert merged["files"][-1]["source"] == "model"
    assert "1 invented path(s) dropped" in merged["grounding"], merged["grounding"]

    # a model that answers with nothing usable is not an error
    empty = llm.semantic_predict(repo, "anything", StubProvider("no idea, sorry"))
    assert empty["files"] == [] and empty["invented"] == []


def check_brief(repo):
    """The cached half must be byte-identical between runs, and the task half
    must carry everything that differs."""
    rate = mergemind.predict(repo, "Add rate limiting to the API")
    auth = mergemind.predict(repo, "Add authentication middleware")
    found = mergemind.risks(repo, [rate, auth])
    data = agent.brief(repo, [rate, auth], found)

    # a prefix that differs between calls is a cache miss, every time
    again = agent.brief(repo, [rate, auth], found)
    assert data["prefix"] == again["prefix"]
    assert agent.stable_prefix(repo) == agent.stable_prefix(repo)

    # the stable half says nothing about either task
    assert "rate limiting" not in data["prefix"]
    assert "authentication" not in data["prefix"]
    assert "api/middleware.py" in data["prefix"]  # it is load-bearing

    # the volatile half carries the task, its files and its coordination
    first = data["suffixes"][0]
    assert first["task"] == "Add rate limiting to the API"
    assert "rate_limit(request, limit)" in first["text"]
    assert "Agree on who owns" in first["text"]
    assert data["suffixes"][1]["text"] != first["text"]

    # this fixture is tiny, so it cannot clear the cache floor and must say so
    assert not data["cacheable"]
    assert data["warnings"] and "do not apply" in data["warnings"][0]

    # the request puts the breakpoint on the repo half, not the task half
    req = agent.request_skeleton(data)
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["system"][0]["text"] == data["prefix"]
    assert req["messages"][0]["content"] == first["text"]
    assert "cache_control" not in req["messages"][0]


def check_backfill(root):
    """Land a real conflicting merge, then replay it and check we would have
    called it in advance, without the replay ever seeing the merge commit."""
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                    capture_output=True)
    target = root / "api/middleware.py"

    run("checkout", "-q", "limits")
    merged = subprocess.run(["git", "-C", str(root), "merge", "--no-commit", "buckets"],
                            capture_output=True, text=True)
    assert merged.returncode != 0, "fixture should conflict"
    target.write_text("def authenticate(request, token):\n    return bool(token)\n\n"
                      "def rate_limit(request, limit, window, bucket):\n    return True\n")
    run("add", "-A")
    run("commit", "-qm", "merge buckets into limits")
    run("checkout", "-q", "main")

    found = backfill.merge_commits(root, 10, ref="limits")
    assert len(found) == 1, found

    result = backfill.replay_one(root, found[0])
    assert result["conflicted"] == ["api/middleware.py"], result
    assert result["caught"] == ["api/middleware.py"], result
    assert not result["missed"], result
    assert result["levels"]["api/middleware.py"] in ("medium", "high"), result
    assert result["lead_seconds"] >= 0

    # a sample this small must refuse to produce a rate
    summary = backfill.report([result])
    assert summary["sample_too_small"]
    assert "anecdote" in summary["verdict"], summary
    assert "sanity check" in summary["recall_note"]

    # with enough runs it reports, and it calls an inverted score inverted
    inverted = [dict(result, merge=f"x{i}", conflicted=["a.py"], caught=["a.py"],
                     levels={"a.py": "medium", "b.py": "high"},
                     flagged_no_conflict=["b.py"], predicted_files=["a.py", "b.py"])
                for i in range(12)]
    summary = backfill.report(inverted)
    assert not summary["sample_too_small"]
    assert summary["by_level"]["high (code)"]["rate"] == 0.0
    assert summary["by_level"]["medium (code)"]["rate"] == 1.0
    assert "inverted" in summary["verdict"], summary["verdict"]

    # lockfiles and changelogs are kept out of the source-file comparison, so a
    # changelog that conflicts every time cannot make the score look broken
    noisy = [dict(result, merge=f"y{i}", conflicted=["CHANGES.rst"],
                  caught=["CHANGES.rst"], predicted_files=["CHANGES.rst", "b.py"],
                  levels={"CHANGES.rst": "medium", "b.py": "high"},
                  flagged_no_conflict=["b.py"])
             for i in range(12)]
    summary = backfill.report(noisy)
    assert summary["by_level"]["medium (non-code)"]["rate"] == 1.0
    assert "inverted" not in summary["verdict"], summary["verdict"]
    assert "Only one risk level" in summary["verdict"], summary["verdict"]


def check_branches(root, repo):
    """Two branches that edit the same signature: forecast it, then merge it
    for real and see whether the forecast was right."""
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                    capture_output=True)
    target = root / "api/middleware.py"
    original = target.read_text()

    run("checkout", "-qb", "limits", "main")
    target.write_text(original.replace("def rate_limit(request, limit=100):",
                                       "def rate_limit(request, limit, window):"))
    run("commit", "-qam", "window argument")

    run("checkout", "-qb", "buckets", "main")
    target.write_text(original.replace("def rate_limit(request, limit=100):",
                                       "def rate_limit(request, bucket):"))
    run("commit", "-qam", "bucket argument")
    run("checkout", "-q", "main")

    found = branches.branches(root, "main")
    assert set(found) == {"limits", "buckets"}, found
    limits = found["limits"]
    assert limits["ahead"] == 1 and limits["behind"] == 0, limits
    assert limits["signature_changes"] == [{
        "file": "api/middleware.py", "symbol": "rate_limit",
        "before": "rate_limit(request, limit)",
        "after": "rate_limit(request, limit, window)",
        "kind": "changed",
    }], limits["signature_changes"]

    # a signature change with callers is a risk on its own, no second task needed
    forecasts = [branches.as_forecast(b) for b in found.values()]
    observed = mergemind.risks(repo, forecasts)
    sig = [r for r in observed if r["risk_type"] == "api_signature_change"]
    assert len(sig) == 2, observed
    assert "caller" in sig[0]["recommendation"], sig[0]

    # a symbol that leaves one file and lands in another moved; it was not
    # deleted, and a refactor must not read as a pile of removals
    run("checkout", "-qb", "relocate", "main")
    (root / "api/limits.py").write_text(
        "def rate_limit(request, limit=100):\n    return True\n")
    (root / "api/middleware.py").write_text(
        "def authenticate(request, token):\n    return bool(token)\n")
    run("add", "-A")
    run("commit", "-qm", "move rate_limit out of middleware")
    run("checkout", "-q", "main")

    moved = branches.branch(root, "relocate", "main")["signature_changes"]
    assert [c["kind"] for c in moved] == ["moved"], moved
    assert moved[0]["moved_to"] == "api/limits.py", moved
    assert "moved to api/limits.py" in moved[0]["after"]

    # and the two branches really do collide, in the file we said they would
    outcome = merge.trial_merge(root, "limits", "buckets")
    assert not outcome["merged_clean"], outcome
    assert outcome["conflicted_files"] == ["api/middleware.py"], outcome
    result = merge.compare(observed, outcome, forecasts)
    assert result["predicted_and_conflicted"] == ["api/middleware.py"], result
    assert not result["conflicted_unpredicted"], result

    # merging one branch alone is clean, and we say so without claiming a win
    clean = merge.trial_merge(root, "main", "limits")
    assert clean["merged_clean"], clean
    assert "never the claim" in " ".join(
        merge.compare(observed, clean, forecasts)["notes"])

    # the worktree is gone afterwards, whatever happened
    worktrees = subprocess.run(["git", "-C", str(root), "worktree", "list"],
                               capture_output=True, text=True).stdout
    assert worktrees.count("\n") == 1, worktrees

    # risks survive a round trip, so `explain <id>` works on a later run
    db = store.connect(root)
    store.save_risks(db, observed)
    back = store.get_risk(db, sig[0]["id"])
    assert back["evidence"] == sig[0]["evidence"], back
    assert store.get_risk(db, "Rnope") is None
    store.save_outcome(db, repo, outcome, result)
    assert store.insights(db)["merges_run"] == 1
    # a merge run by hand is not history; only backfill can grade the forecast
    assert store.insights(db)["merges_replayed"] == 0
    assert "no history replayed yet" in store.insights(db)["accuracy"]
    store.save_outcome(db, repo, outcome, result, source="backfill")
    assert store.insights(db)["merges_run"] == 1
    assert store.insights(db)["merges_replayed"] == 1


if __name__ == "__main__":
    main()
