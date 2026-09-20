"""A small, realistic repository where the four scenarios actually happen.

Not a toy with one file: an app with a stored user model, an auth path, a
signup API, a frontend form and tests, because the interesting failures only
appear when a change crosses between those layers.

Three branches are laid down on top of it, each individually reasonable, which
together break the same contract from three different sides.
"""

import subprocess
from pathlib import Path

FILES = {
    "app/models.py": '''"""The stored shape of a user."""


class User:
    def __init__(self, id, name, email=None):
        self.id = id
        self.name = name
        self.email = email

    def to_dict(self):
        return {"id": self.id, "name": self.name, "email": self.email}


def create_user(id, name, email=None):
    """Build a user. Email has always been optional here."""
    return User(id, name, email)
''',
    "app/auth.py": '''from app.models import User, create_user


def login(session, name):
    """Look a user up by name and put them in the session."""
    user = create_user(len(session), name)
    session["user"] = user.to_dict()
    return user


def current_user(session):
    return session.get("user")
''',
    "app/billing.py": '''from app.models import User


def charge(user, cents):
    """Charge a user. Needs a contactable address for the receipt."""
    return {"user": user.id, "cents": cents, "receipt_to": user.email}
''',
    "app/notify.py": '''from app.models import User


def send_welcome(user):
    if not user.email:
        return False
    return True
''',
    "api/signup.py": '''from app.auth import login
from app.models import create_user


def signup(request):
    """Create an account from whatever the signup form posted."""
    user = create_user(request["id"], request["name"])
    login(request.setdefault("session", {}), request["name"])
    return {"id": user.id, "name": user.name}
''',
    "web/signup_form.js": '''import { post } from "./client.js";

export function submitSignup(form) {
  return post("/signup", { id: form.id, name: form.name, email: form.email });
}
''',
    "web/client.js": '''export function post(path, body) {
  return fetch(path, { method: "POST", body: JSON.stringify(body) });
}
''',
    "migrations/0001_create_users.sql": (
        "CREATE TABLE users (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  name TEXT NOT NULL,\n"
        "  email TEXT\n"
        ");\n"
    ),
    "tests/test_signup.py": '''from api.signup import signup


def test_signup_without_email():
    out = signup({"id": 1, "name": "ada"})
    assert out["name"] == "ada"
''',
    "tests/test_models.py": '''from app.models import create_user


def test_create_user_email_optional():
    user = create_user(1, "ada")
    assert user.email is None
''',
    ".gitignore": ".prophecy/\n__pycache__/\n",
    "app/db.py": '''"""Connection handling. Everything that persists goes through here."""


def connect(url, timeout=5):
    return {"url": url, "timeout": timeout}


def query(conn, sql, args=()):
    return []
''',
    "app/scoring.py": '''from app.models import User


def score(submission, rubric, judge):
    """Weighted score for one submission from one judge."""
    total = sum(rubric.get(k, 0) * v for k, v in submission.items())
    return {"judge": judge.id, "total": total}


def rank(scores):
    return sorted(scores, key=lambda s: -s["total"])
''',
    "api/teams.py": '''from app.db import query
from app.models import create_user


def create_team(request, conn):
    """Register a team and its first member."""
    owner = create_user(request["id"], request["name"])
    query(conn, "INSERT INTO teams (id, owner) VALUES (?, ?)")
    return {"team": request["id"], "owner": owner.id}


def list_teams(conn):
    return query(conn, "SELECT * FROM teams")
''',
    "api/submissions.py": '''from app.db import query
from app.models import User
from app.scoring import score


def submit(request, conn):
    """Take a project submission from the web form."""
    query(conn, "INSERT INTO submissions (team, repo) VALUES (?, ?)")
    return {"team": request["team"], "repo": request["repo"]}


def tally(submission, rubric, judge):
    return score(submission, rubric, judge)
''',
    "api/leaderboard.py": '''from app.db import query
from app.scoring import rank


def leaderboard(conn, limit=20):
    rows = query(conn, "SELECT team, total FROM scores")
    return rank(rows)[:limit]
''',
    "workers/emailer.py": '''from app.models import User
from app.notify import send_welcome


def run(queue):
    """Drain the outbound queue."""
    for user in queue:
        send_welcome(user)
    return len(queue)
''',
    "workers/rescore.py": '''from app.db import connect, query
from app.scoring import rank, score


def rescore_all(url, rubric):
    """Recompute every score after a rubric change."""
    conn = connect(url)
    return rank([score(row, rubric, row) for row in query(conn, "SELECT *")])
''',
    "web/leaderboard.js": '''import { post } from "./client.js";

export function loadBoard() {
  return post("/leaderboard", { limit: 20 });
}
''',
    "tests/test_scoring.py": '''from app.scoring import rank, score


def test_rank_orders_by_total():
    assert rank([{"total": 1}, {"total": 9}])[0]["total"] == 9
''',
    "tests/test_teams.py": '''from api.teams import create_team


def test_create_team():
    out = create_team({"id": 1, "name": "ada"}, None)
    assert out["team"] == 1
''',
    ".github/workflows/ci.yml": (
        "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v4\n"
        "      - run: pytest\n"
    ),
    "requirements.txt": "flask==3.0.0\npytest==8.0.0\n",
    "poetry.lock": "# generated; do not edit by hand\nflask = \"3.0.0\"\n",
    "README.md": "# demo app\n\nA small account service, used to show what "
                 "prophecy does when three people change one contract.\n",
}

# Each branch is a reasonable thing for one person to be doing. The damage is
# in the combination, which is the point.
BRANCHES = {
    "agent-a/require-email": {
        "message": "Make email required during signup",
        "author": "ada",
        "edits": {
            "app/models.py": lambda s: s
                .replace("def __init__(self, id, name, email=None):",
                         "def __init__(self, id, name, email):")
                .replace("def create_user(id, name, email=None):",
                         "def create_user(id, name, email):")
                .replace('"""Build a user. Email has always been optional here."""',
                         '"""Build a user. Email is required now."""'),
            "migrations/0002_email_required.sql":
                lambda s: "ALTER TABLE users ALTER COLUMN email SET NOT NULL;\n",
        },
    },
    "agent-b/signup-redesign": {
        "message": "Redesign signup, drop the fields nobody fills in",
        "author": "grace",
        "edits": {
            "web/signup_form.js": lambda s: s.replace(
                'return post("/signup", { id: form.id, name: form.name, '
                'email: form.email });',
                'return post("/signup", { id: form.id, name: form.name });'),
            "api/signup.py": lambda s: s.replace(
                'def signup(request):',
                'def signup(request, source="web"):'),
        },
    },
    "agent-c/user-cleanup": {
        "message": "Tidy up the user table",
        "author": "linus",
        "edits": {
            "migrations/0003_drop_legacy.sql":
                lambda s: "DELETE FROM users WHERE email IS NULL;\n",
            "app/notify.py": lambda s: s.replace(
                "def send_welcome(user):", "def send_welcome(user, template):"),
        },
    },
}


# Work that has already landed, so the project has a history to replay and a
# track record to read. Each of these is merged into main.
LANDED = [
    {"branch": "feat/leaderboard-cache", "author": "priya",
     "message": "Cache the leaderboard for 30 seconds",
     "edits": {"api/leaderboard.py": lambda s: s.replace(
         "def leaderboard(conn, limit=20):",
         "def leaderboard(conn, limit=20, ttl=30):")}},
    {"branch": "fix/scoring-tie", "author": "sam",
     "message": "Break ties by submission time",
     "edits": {"app/scoring.py": lambda s: s.replace(
         "return sorted(scores, key=lambda s: -s[\"total\"])",
         "return sorted(scores, key=lambda s: (-s[\"total\"], s.get(\"at\", 0)))")}},
    {"branch": "chore/bump-flask", "author": "priya",
     "message": "Bump flask",
     "edits": {"requirements.txt": lambda s: s.replace("3.0.0", "3.0.2"),
               "poetry.lock": lambda s: s.replace("3.0.0", "3.0.2")}},
    {"branch": "feat/worker-retries", "author": "sam",
     "message": "Retry failed sends instead of dropping them",
     "edits": {"workers/emailer.py": lambda s: s.replace(
         "def run(queue):", "def run(queue, retries=3):")}},
]

# Still open, and this is where the interesting collision is
EXTRA_OPEN = {
    "feat/judge-dashboard": {
        "message": "Add a judging dashboard",
        "author": "priya",
        "edits": {
            "api/judging.py": lambda s: (
                "from app.scoring import rank, score\n\n\n"
                "def board(conn, rubric):\n"
                "    \"\"\"Everything a judge needs on one screen.\"\"\"\n"
                "    return rank([])\n"),
            "web/leaderboard.js": lambda s: s.replace(
                "export function loadBoard() {",
                "export function loadBoard(judgeId) {"),
        },
    },
    "chore/drop-dead-helper": {
        "message": "Delete a helper nothing calls",
        "author": "sam",
        "edits": {"app/db.py": lambda s: s.replace(
            "\n\ndef query(conn, sql, args=()):\n    return []\n",
            "\n\ndef query(conn, sql, args=()):\n    return []\n\n\n"
            "def _unused_debug(conn):\n    return conn\n")},
    },
}


def run(root, *args):
    return subprocess.run(args, cwd=str(root), capture_output=True,
                          text=True, check=True)


def build(path):
    root = Path(path).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        return {"error": f"{root} already exists and is not empty."}
    root.mkdir(parents=True, exist_ok=True)

    for rel, body in FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)

    run(root, "git", "init", "-q")
    run(root, "git", "config", "user.email", "demo@example.com")
    run(root, "git", "config", "user.name", "demo")
    run(root, "git", "add", "-A")
    run(root, "git", "commit", "-qm", "An account service")

    # land some work first, with real merge commits, so there is history to
    # replay and a record to read on day one
    for spec in LANDED:
        run(root, "git", "checkout", "-q", "-b", spec["branch"], "main")
        for rel, edit in spec["edits"].items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit(target.read_text() if target.exists() else ""))
        run(root, "git", "add", "-A")
        run(root, "git", "-c", f"user.name={spec['author']}",
            "-c", f"user.email={spec['author']}@example.com",
            "commit", "-qm", spec["message"])
        run(root, "git", "checkout", "-q", "main")
        run(root, "git", "-c", "user.name=demo", "-c", "user.email=demo@example.com",
            "merge", "--no-ff", "-q", spec["branch"],
            "-m", f"Merge {spec['branch']}")
        run(root, "git", "branch", "-q", "-D", spec["branch"])

    # One merge in the history that really did conflict, so replaying the
    # past has something to grade the forecast against. Both sides edit the
    # same line of rank(), which is exactly the case prophecy claims to see
    # coming.
    base = run(root, "git", "rev-parse", "HEAD").stdout.strip()
    for who, replacement in (
        ("priya", '    return sorted(scores, key=lambda s: (-s["total"], s.get("at", 0)))\n'
                  '\n\ndef top(scores, n=3):\n    return rank(scores)[:n]\n'),
        ("sam", '    return sorted(scores, key=lambda s: (-s["total"], s.get("team", "")))\n'
                '\n\ndef bottom(scores, n=3):\n    return rank(scores)[-n:]\n'),
    ):
        run(root, "git", "checkout", "-q", "-b", f"rank/{who}", base)
        target = root / "app/scoring.py"
        body = target.read_text()
        head, _, _ = body.partition("    return sorted(scores")
        target.write_text(head + replacement)
        run(root, "git", "add", "-A")
        run(root, "git", "-c", f"user.name={who}",
            "-c", f"user.email={who}@example.com", "commit", "-qm",
            f"Change how rank breaks ties ({who})")
        run(root, "git", "checkout", "-q", "main")

    run(root, "git", "-c", "user.name=demo", "-c", "user.email=demo@example.com",
        "merge", "--no-ff", "-q", "rank/priya", "-m", "Merge rank/priya")
    clash = subprocess.run(
        ["git", "merge", "--no-ff", "-m", "Merge rank/sam", "rank/sam"],
        cwd=str(root), capture_output=True, text=True)
    if clash.returncode:  # resolve it the way a person would, by hand
        (root / "app/scoring.py").write_text(
            (root / "app/scoring.py").read_text()
            .split("<<<<<<<")[0]
            + '    return sorted(scores, key=lambda s: (-s["total"], s.get("at", 0),\n'
              '                                        s.get("team", "")))\n'
              '\n\ndef top(scores, n=3):\n    return rank(scores)[:n]\n'
              '\n\ndef bottom(scores, n=3):\n    return rank(scores)[-n:]\n')
        run(root, "git", "add", "-A")
        run(root, "git", "-c", "user.name=demo", "-c", "user.email=demo@example.com",
            "commit", "-qm", "Merge rank/sam, keeping both tiebreakers")
    run(root, "git", "branch", "-q", "-D", "rank/priya")
    run(root, "git", "branch", "-q", "-D", "rank/sam")

    made = []
    for name, spec in {**BRANCHES, **EXTRA_OPEN}.items():
        run(root, "git", "checkout", "-q", "-b", name, "main")
        for rel, edit in spec["edits"].items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit(target.read_text() if target.exists() else ""))
        run(root, "git", "add", "-A")
        run(root, "git", "-c", f"user.name={spec['author']}",
            "-c", f"user.email={spec['author']}@example.com",
            "commit", "-qm", spec["message"])
        made.append({"branch": name, "author": spec["author"],
                     "message": spec["message"]})
    run(root, "git", "checkout", "-q", "main")
    return {"path": str(root), "branches": made}


# Two agents already at work, so the team view is not an empty table during a
# demo. Both are real sessions written through the normal path — nothing here
# fakes a number that the rest of the app would not have produced.
SEED_SESSIONS = [
    {"agent": "ada", "task": "Make email required during signup",
     "note": ("app/models.py",
              "create_user has five importers; every one of them omits email "
              "today, so the default cannot just be dropped")},
    {"agent": "grace", "task": "Redesign the signup form",
     "note": ("api/signup.py",
              "signup() builds the user directly rather than going through "
              "the auth path, so changes here bypass login()")},
]


def seed(root):
    """Give the project a history so the first screen has something in it."""
    from . import store as store_mod
    from .agent import brief, observations
    from .predict import predict
    from .risk import risks
    from .scan import scan
    from .work import in_flight

    repo = scan(root)
    db = store_mod.connect(root)

    for person in ("ada", "grace", "linus"):
        store_mod.add_member(db, person, github=person)

    forecasts = []
    for entry in SEED_SESSIONS:
        agent, task = entry["agent"], entry["task"]
        store_mod.join_session(db, store_mod.session_id(root, agent),
                               repo["sha"], agent, task, "mcp")
        forecast = predict(repo, task)
        forecasts.append(forecast)
        found = risks(repo, forecasts)
        data = brief(repo, [forecast], found, db=db, agent=agent,
                     store=store_mod)
        store_mod.log(db, repo["sha"], "joined", agent,
                      f"joined, working on {task}",
                      data["suffixes"][0]["tokens"])
        path, text = entry["note"]
        store_mod.add_note(db, repo["sha"], agent, path, text)
        store_mod.log(db, repo["sha"], "shared", agent,
                      f"shared something about {path}: {text}")

    # brief everyone once more now that the notes exist, so the findings
    # actually reach the other agent rather than sitting unread
    for entry, forecast in zip(SEED_SESSIONS, forecasts):
        brief(repo, [forecast], risks(repo, forecasts), db=db,
              agent=entry["agent"], store=store_mod)

    work = in_flight(repo, "main", db, store_mod)
    for note in observations(repo, work):
        if not store_mod.note_exists(db, note["file"], note["note"]):
            store_mod.add_note(db, repo["sha"], note["agent"], note["file"],
                               note["note"])
            store_mod.log(db, repo["sha"], "shared", "prophecy",
                          f"noted about {note['file']}: {note['note']}")
    return {"sessions": len(SEED_SESSIONS), "people": 3}
