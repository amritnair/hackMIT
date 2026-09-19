"""Builds a small throwaway repo and checks the whole pipeline against it.

Run with: python test_mergemind.py
"""

import subprocess
import tempfile
from pathlib import Path

import mergemind
from mergemind import backfill, branches, merge, store

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

    print("ok")


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

    # with enough runs it does report, and it reports the ordering honestly
    inverted = [dict(result, merge=f"x{i}", conflicted=["a.py"], caught=["a.py"],
                     levels={"a.py": "medium", "b.py": "high"},
                     flagged_no_conflict=["b.py"], predicted_files=["a.py", "b.py"])
                for i in range(12)]
    summary = backfill.report(inverted)
    assert not summary["sample_too_small"]
    assert summary["by_level"]["high"]["rate"] == 0.0
    assert summary["by_level"]["medium"]["rate"] == 1.0
    assert "inverted" in summary["verdict"], summary["verdict"]


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
    }], limits["signature_changes"]

    # a signature change with callers is a risk on its own, no second task needed
    forecasts = [branches.as_forecast(b) for b in found.values()]
    observed = mergemind.risks(repo, forecasts)
    sig = [r for r in observed if r["risk_type"] == "api_signature_change"]
    assert len(sig) == 2, observed
    assert "caller" in sig[0]["recommendation"], sig[0]

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
    assert "not enough" in store.insights(db)["accuracy"]


if __name__ == "__main__":
    main()
