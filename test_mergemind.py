"""Builds a small throwaway repo and checks the whole pipeline against it.

Run with: python test_mergemind.py
"""

import subprocess
import tempfile
from pathlib import Path

import mergemind

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
        assert top["id"] == "R1"

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

    print("ok")


if __name__ == "__main__":
    main()
