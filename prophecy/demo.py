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

from app.config import database_url


def connect(url=None, timeout=5):
    url = url or database_url()
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

# The project has to be big enough to be worth talking about. A nine-file toy
# makes every number on the context tab look like a rounding error, and the
# cached-prefix argument does not even apply below a thousand tokens of shared
# background — so the demo is a real app's worth of surface, with the same
# contract running through it.
FILES.update({
    "app/validators.py": '''"""Everything that decides whether input is allowed in.

Called from the signup path, the admin path and the importer, which is why a
rule changing here is never local to one caller.
"""

import re

EMAIL = re.compile(r"^[^@\\s]+@[^@\\s]+\\.[a-z]{2,}$", re.I)
HANDLE = re.compile(r"^[a-z0-9_-]{2,32}$", re.I)


def valid_email(value):
    """True for something we could actually send a receipt to."""
    return bool(value and EMAIL.match(value.strip()))


def valid_handle(value):
    return bool(value and HANDLE.match(value.strip()))


def normalise_email(value):
    return (value or "").strip().lower()


def check_signup(payload):
    """Collect every problem at once, rather than failing on the first."""
    problems = []
    if not payload.get("name"):
        problems.append("name is required")
    if payload.get("email") and not valid_email(payload["email"]):
        problems.append("that email address does not look deliverable")
    if payload.get("handle") and not valid_handle(payload["handle"]):
        problems.append("handles are letters, numbers, dash and underscore")
    return problems


def require(payload, *fields):
    missing = [f for f in fields if not payload.get(f)]
    if missing:
        raise ValueError("missing: " + ", ".join(missing))
    return payload
''',
    "app/sessions.py": '''"""Session storage. Thin on purpose: the rules live in permissions.py."""

import time

TTL_SECONDS = 60 * 60 * 12


def new_session(user, now=None):
    return {"user": user.to_dict(), "at": now or time.time()}


def expired(session, now=None):
    if not session or "at" not in session:
        return True
    return (now or time.time()) - session["at"] > TTL_SECONDS


def touch(session, now=None):
    session["at"] = now or time.time()
    return session


def user_of(session):
    return (session or {}).get("user")
''',
    "app/permissions.py": '''"""Who is allowed to do what.

Reads the user dict that sessions hand out, so a change to the stored shape of
a user lands here too.
"""

from app.sessions import user_of

ROLES = ("entrant", "judge", "organiser")


def role_of(session):
    user = user_of(session) or {}
    return user.get("role", "entrant")


def can_judge(session):
    return role_of(session) in ("judge", "organiser")


def can_admin(session):
    return role_of(session) == "organiser"


def owns(session, record):
    user = user_of(session) or {}
    return record.get("owner") == user.get("id")


def assert_can(session, action):
    allowed = {"judge": can_judge, "admin": can_admin}.get(action)
    if allowed and not allowed(session):
        raise PermissionError(f"not allowed to {action}")
    return True
''',
    "app/audit.py": '''"""An append-only record of things that changed, for the organisers."""

from app.db import query


def record(conn, actor, action, subject, detail=""):
    query(conn, "INSERT INTO audit (actor, action, subject, detail)"
                " VALUES (?, ?, ?, ?)")
    return {"actor": actor, "action": action, "subject": subject,
            "detail": detail}


def for_subject(conn, subject):
    return query(conn, "SELECT * FROM audit WHERE subject = ? ORDER BY id DESC")


def recent(conn, limit=50):
    return query(conn, "SELECT * FROM audit ORDER BY id DESC LIMIT ?")
''',
    "services/rubric.py": '''"""The rubric a submission is scored against.

Weights are stored, not hard-coded, because organisers change them mid-event
and every score already given has to be recomputed when they do.
"""

DEFAULT = {"idea": 0.3, "execution": 0.4, "impact": 0.2, "presentation": 0.1}


def normalise(weights):
    """Make the weights sum to one, so totals stay comparable."""
    total = sum(weights.values()) or 1
    return {k: v / total for k, v in weights.items()}


def validate(weights):
    problems = []
    for key, value in weights.items():
        if value < 0:
            problems.append(f"{key} cannot be negative")
    if not weights:
        problems.append("a rubric needs at least one criterion")
    return problems


def merge(base, overrides):
    out = dict(base)
    out.update(overrides or {})
    return normalise(out)


def criteria(weights=None):
    return sorted((weights or DEFAULT).keys())
''',
    "services/matchmaking.py": '''"""Assigning submissions to judges without anyone judging their own team."""

from app.permissions import can_judge


def eligible(judges, submission):
    return [j for j in judges
            if j.get("team") != submission.get("team")]


def assign(submissions, judges, per_submission=2):
    """Spread the work as evenly as the constraints allow."""
    load = {j["id"]: 0 for j in judges}
    out = {}
    for submission in submissions:
        pool = sorted(eligible(judges, submission), key=lambda j: load[j["id"]])
        picked = pool[:per_submission]
        out[submission["id"]] = [j["id"] for j in picked]
        for judge in picked:
            load[judge["id"]] += 1
    return out


def unassigned(assignments, submissions):
    return [s["id"] for s in submissions if not assignments.get(s["id"])]
''',
    "services/payouts.py": '''"""Prize payouts. Needs a contactable address for every winner."""

from app.config import prize_pot
from app.models import User
from app.notify import send_welcome
from services.notifications import deliverable


def winners(board, places=3):
    return board[:places]


def payout_for(place, pot):
    split = {0: 0.5, 1: 0.3, 2: 0.2}.get(place, 0)
    return round(pot * split, 2)


def prepare(board, pot):
    """Build the payout list. A winner with no email cannot be paid."""
    out = []
    for place, row in enumerate(winners(board)):
        out.append({"team": row.get("team"),
                    "amount": payout_for(place, pot),
                    "contactable": bool(row.get("email"))})
    return out


def notify_winners(users):
    return sum(1 for user in users if send_welcome(user))
''',
    "api/admin.py": '''from app.audit import record
from app.errors import bad_request, ok
from app.permissions import assert_can
from app.db import query
from services.rubric import merge, validate


def update_rubric(request, conn, session):
    """Change the weights mid-event, and write down who did it."""
    assert_can(session, "admin")
    problems = validate(request.get("weights", {}))
    if problems:
        return {"ok": False, "problems": problems}
    weights = merge({}, request["weights"])
    query(conn, "UPDATE rubric SET weights = ?")
    record(conn, session.get("user", {}).get("id"), "rubric", "event", str(weights))
    return {"ok": True, "weights": weights}


def disqualify(request, conn, session):
    assert_can(session, "admin")
    query(conn, "UPDATE submissions SET disqualified = 1 WHERE team = ?")
    record(conn, session.get("user", {}).get("id"), "disqualify",
           request["team"], request.get("reason", ""))
    return {"ok": True, "team": request["team"]}
''',
    "api/profile.py": '''from app.errors import bad_request, ok
from app.models import User
from app.validators import check_signup, normalise_email
from app.sessions import user_of


def update_profile(request, session):
    """Let somebody correct their own details after signing up."""
    problems = check_signup(request)
    if problems:
        return {"ok": False, "problems": problems}
    current = user_of(session) or {}
    return {"ok": True, "id": current.get("id"),
            "name": request.get("name", current.get("name")),
            "email": normalise_email(request.get("email"))}


def show_profile(session):
    user = user_of(session) or {}
    return {"id": user.get("id"), "name": user.get("name"),
            "email": user.get("email")}
''',
    "workers/digest.py": '''"""The nightly digest. Reads users, so it reads the user contract."""

from app.config import load
from app.db import connect, query
from app.models import User
from app.notify import send_welcome
from services.notifications import send_many


def recipients(conn):
    return query(conn, "SELECT * FROM users WHERE digest = 1")


def build(rows):
    return {"count": len(rows), "subject": f"{len(rows)} updates today"}


def run(url):
    conn = connect(url)
    rows = recipients(conn)
    sent = sum(1 for row in rows if send_welcome(row))
    return {"considered": len(rows), "sent": sent}
''',
    "workers/reminders.py": '''"""Nudges judges who have not finished scoring."""

from app.db import connect, query
from services.matchmaking import unassigned


def outstanding(conn):
    return query(conn, "SELECT * FROM assignments WHERE score IS NULL")


def run(url, submissions=()):
    conn = connect(url)
    rows = outstanding(conn)
    return {"pending": len(rows),
            "unassigned": len(unassigned({}, list(submissions)))}
''',
    "web/admin.js": '''import { post } from "./client.js";

export function saveRubric(weights) {
  return post("/admin/rubric", { weights });
}

export function disqualify(team, reason) {
  return post("/admin/disqualify", { team, reason });
}
''',
    "web/judge.js": '''import { post } from "./client.js";

export function loadQueue(judgeId) {
  return post("/judge/queue", { judgeId });
}

export function submitScore(submissionId, scores) {
  return post("/judge/score", { submissionId, scores });
}
''',
    "web/profile_form.js": '''import { post } from "./client.js";

export function saveProfile(form) {
  return post("/profile", { name: form.name, email: form.email });
}
''',
    "tests/test_validators.py": '''from app.validators import check_signup, valid_email, valid_handle


def test_email_shapes():
    assert valid_email("ada@example.com")
    assert not valid_email("ada@example")


def test_handle_shapes():
    assert valid_handle("ada_1")
    assert not valid_handle("a")


def test_signup_collects_every_problem():
    problems = check_signup({"email": "nope", "handle": "!"})
    assert len(problems) == 3
''',
    "tests/test_permissions.py": '''from app.permissions import can_admin, can_judge, role_of


def test_default_role_is_entrant():
    assert role_of({"user": {"id": 1}}) == "entrant"


def test_judge_can_judge():
    assert can_judge({"user": {"role": "judge"}})
    assert not can_admin({"user": {"role": "judge"}})
''',
    "tests/test_rubric.py": '''from services.rubric import merge, normalise, validate


def test_weights_normalise_to_one():
    out = normalise({"a": 1, "b": 3})
    assert round(sum(out.values()), 6) == 1


def test_negative_weight_is_a_problem():
    assert validate({"a": -1})


def test_merge_keeps_base_keys():
    assert "idea" in merge({"idea": 1}, {"impact": 1})
''',
    "tests/test_matchmaking.py": '''from services.matchmaking import assign, eligible


def test_nobody_judges_their_own_team():
    judges = [{"id": 1, "team": "a"}, {"id": 2, "team": "b"}]
    assert [j["id"] for j in eligible(judges, {"team": "a"})] == [2]


def test_assignment_spreads_load():
    judges = [{"id": 1, "team": "x"}, {"id": 2, "team": "y"}]
    out = assign([{"id": "s1", "team": "a"}, {"id": "s2", "team": "b"}], judges, 1)
    assert sorted(sum(out.values(), [])) == [1, 2]
''',
    "tests/test_payouts.py": '''from services.payouts import payout_for, prepare


def test_first_place_takes_half():
    assert payout_for(0, 1000) == 500.0


def test_uncontactable_winner_is_flagged():
    out = prepare([{"team": "a"}], 100)
    assert out[0]["contactable"] is False
''',
    "app/errors.py": '''"""The error shapes the API returns.

Imported by every route, which is what makes renaming one of these a change
with a blast radius rather than a rename.
"""


class DomainError(Exception):
    """Something the caller did wrong, safe to show them."""


def bad_request(problems):
    return {"ok": False, "status": 400, "problems": list(problems)}


def forbidden(action):
    return {"ok": False, "status": 403,
            "problems": [f"not allowed to {action}"]}


def not_found(what):
    return {"ok": False, "status": 404, "problems": [f"no such {what}"]}


def conflict(detail):
    return {"ok": False, "status": 409, "problems": [detail]}


def ok(payload=None):
    return {"ok": True, "status": 200, "data": payload or {}}
''',
    "app/config.py": '''"""Settings, read once at start-up.

Everything that talks to the outside world reads from here, so a renamed key
is felt in the workers and the API at the same time.
"""

DEFAULTS = {
    "database_url": "sqlite:///demo.db",
    "from_address": "noreply@example.com",
    "prize_pot": 5000,
    "judges_per_submission": 2,
    "digest_hour": 7,
}


def load(overrides=None):
    settings = dict(DEFAULTS)
    settings.update(overrides or {})
    return settings


def database_url(settings=None):
    return (settings or DEFAULTS)["database_url"]


def prize_pot(settings=None):
    return (settings or DEFAULTS)["prize_pot"]


def from_address(settings=None):
    return (settings or DEFAULTS)["from_address"]


def judges_per_submission(settings=None):
    return (settings or DEFAULTS)["judges_per_submission"]
''',
    "services/notifications.py": '''"""One place that decides how somebody is contacted.

Reads the user contract to find an address, so it is downstream of any change
to how a user is stored.
"""

from app.config import from_address
from app.models import User


def address_for(user):
    """The address to use, or None when there is nothing to send to."""
    return (user or {}).get("email") if isinstance(user, dict) else user.email


def deliverable(user):
    return bool(address_for(user))


def compose(user, subject, body):
    return {"to": address_for(user), "from": from_address(),
            "subject": subject, "body": body}


def send(user, subject, body):
    if not deliverable(user):
        return {"sent": False, "reason": "no address"}
    return {"sent": True, "message": compose(user, subject, body)}


def send_many(users, subject, body):
    return [send(u, subject, body) for u in users]
''',
    "app/repository.py": '''"""Every read of a user goes through here.

One place, so a change to the stored shape of a user has one place to be
fixed — and so prophecy has something honest to point at when it says a
change is load-bearing.
"""

from app.db import query
from app.models import User, create_user


def by_id(conn, user_id):
    rows = query(conn, "SELECT * FROM users WHERE id = ?")
    return rows[0] if rows else None


def by_email(conn, email):
    rows = query(conn, "SELECT * FROM users WHERE email = ?")
    return rows[0] if rows else None


def by_handle(conn, handle):
    rows = query(conn, "SELECT * FROM users WHERE handle = ?")
    return rows[0] if rows else None


def insert(conn, payload):
    user = create_user(payload["id"], payload["name"], payload.get("email"))
    query(conn, "INSERT INTO users (id, name, email) VALUES (?, ?, ?)")
    return user


def update_email(conn, user_id, email):
    query(conn, "UPDATE users SET email = ? WHERE id = ?")
    return {"id": user_id, "email": email}


def all_users(conn, limit=200):
    return query(conn, "SELECT * FROM users LIMIT ?")
''',
    "app/serialisers.py": '''"""Turning records into the shapes the API promises.

The web client reads these keys by name, so renaming one is an API change
whether or not anybody calls it that.
"""

from app.models import User


def user_json(user):
    return {"id": user.get("id"), "name": user.get("name"),
            "email": user.get("email"), "handle": user.get("handle")}


def team_json(team, members=()):
    return {"id": team.get("id"), "name": team.get("name"),
            "members": [user_json(m) for m in members]}


def submission_json(submission, scores=()):
    return {"id": submission.get("id"), "team": submission.get("team"),
            "repo": submission.get("repo"),
            "scores": [score_json(s) for s in scores]}


def score_json(score):
    return {"judge": score.get("judge"), "total": score.get("total")}


def error_json(problems):
    return {"ok": False, "problems": list(problems)}
''',
    "services/events.py": '''"""The event itself: when it opens, when judging closes."""

import time

PHASES = ("registration", "building", "judging", "done")


def current_phase(event, now=None):
    now = now or time.time()
    if now < event.get("opens_at", 0):
        return PHASES[0]
    if now < event.get("closes_at", 0):
        return PHASES[1]
    if now < event.get("judging_ends_at", 0):
        return PHASES[2]
    return PHASES[3]


def accepting_submissions(event, now=None):
    return current_phase(event, now) in ("registration", "building")


def judging_open(event, now=None):
    return current_phase(event, now) == "judging"


def time_left(event, now=None):
    return max(0, event.get("closes_at", 0) - (now or time.time()))
''',
    "services/leaderboard_service.py": '''"""Assembling the board from scores, with ties already broken."""

from app.scoring import rank
from services.rubric import normalise


def totals(scores, weights):
    weights = normalise(weights)
    out = {}
    for score in scores:
        team = score.get("team")
        out[team] = out.get(team, 0) + score.get("total", 0)
    return out


def board(scores, weights, limit=20):
    rows = [{"team": team, "total": total}
            for team, total in totals(scores, weights).items()]
    return rank(rows)[:limit]


def position_of(board_rows, team):
    for index, row in enumerate(board_rows):
        if row["team"] == team:
            return index + 1
    return None


def published(board_rows, phase):
    return board_rows if phase == "done" else []
''',
    "services/importer.py": '''"""Bulk import, used when an event is run twice.

Goes through the same validators as signup, because an importer that accepts
what the form rejects is how bad rows get in.
"""

from app.errors import bad_request
from app.repository import insert
from app.validators import check_signup, normalise_email


def parse_row(row):
    return {"id": row.get("id"), "name": (row.get("name") or "").strip(),
            "email": normalise_email(row.get("email"))}


def validate_rows(rows):
    problems = {}
    for row in rows:
        found = check_signup(parse_row(row))
        if found:
            problems[row.get("id")] = found
    return problems


def import_rows(conn, rows):
    problems = validate_rows(rows)
    added = [insert(conn, parse_row(r)) for r in rows
             if r.get("id") not in problems]
    return {"added": len(added), "rejected": len(problems),
            "problems": problems}
''',
    "api/teams_members.py": '''from app.permissions import assert_can, owns
from app.repository import by_id
from app.serialisers import team_json
from app.db import query


def add_member(request, conn, session):
    """Put somebody on a team, if you are allowed to."""
    team = {"id": request["team"], "owner": request.get("owner")}
    if not owns(session, team):
        assert_can(session, "admin")
    query(conn, "INSERT INTO team_members (team, user) VALUES (?, ?)")
    return team_json(team, [by_id(conn, request["user"])])


def remove_member(request, conn, session):
    assert_can(session, "admin")
    query(conn, "DELETE FROM team_members WHERE team = ? AND user = ?")
    return {"ok": True}


def members_of(conn, team_id):
    return query(conn, "SELECT * FROM team_members WHERE team = ?")
''',
    "api/scores.py": '''from app.errors import forbidden, ok
from app.permissions import assert_can
from app.audit import record
from app.scoring import score
from app.db import query
from services.rubric import DEFAULT


def submit_score(request, conn, session):
    """One judge scoring one submission."""
    assert_can(session, "judge")
    result = score(request["scores"], request.get("rubric", DEFAULT),
                   type("J", (), {"id": request["judge"]}))
    query(conn, "UPDATE assignments SET score = ? WHERE submission = ?")
    record(conn, request["judge"], "score", request["submission"])
    return result


def scores_for(conn, submission):
    return query(conn, "SELECT * FROM assignments WHERE submission = ?")


def reopen(request, conn, session):
    assert_can(session, "admin")
    query(conn, "UPDATE assignments SET score = NULL WHERE submission = ?")
    return {"ok": True, "submission": request["submission"]}
''',
    "api/export.py": '''from app.serialisers import submission_json, user_json
from app.repository import all_users
from app.db import query


def export_users(conn):
    return [user_json(u) for u in all_users(conn)]


def export_submissions(conn):
    rows = query(conn, "SELECT * FROM submissions")
    return [submission_json(r) for r in rows]


def export_everything(conn):
    return {"users": export_users(conn),
            "submissions": export_submissions(conn)}
''',
    "workers/exporter.py": '''"""Writes the nightly export somewhere the organisers can fetch it."""

from api.export import export_everything
from app.config import load
from app.db import connect


def filename(day):
    return f"export-{day}.json"


def run(url, day="today"):
    conn = connect(url)
    payload = export_everything(conn)
    return {"file": filename(day), "users": len(payload["users"]),
            "submissions": len(payload["submissions"])}
''',
    "workers/cleanup.py": '''"""Removes the sessions nobody came back to."""

from app.db import connect, query
from app.sessions import expired


def stale(rows, now=None):
    return [r for r in rows if expired(r, now)]


def run(url):
    conn = connect(url)
    rows = query(conn, "SELECT * FROM sessions")
    gone = stale(rows)
    query(conn, "DELETE FROM sessions WHERE id IN (?)")
    return {"removed": len(gone)}
''',
    "web/team_form.js": '''import { post } from "./client.js";

export function createTeam(form) {
  return post("/teams", { name: form.name, owner: form.owner });
}

export function addMember(team, user) {
  return post("/teams/members", { team, user });
}
''',
    "web/score_form.js": '''import { post } from "./client.js";

export function saveScore(submissionId, scores) {
  return post("/scores", { submission: submissionId, scores });
}

export function reopenScore(submissionId) {
  return post("/scores/reopen", { submission: submissionId });
}
''',
    "tests/test_repository.py": '''from app.repository import insert, update_email


def test_insert_builds_a_user():
    user = insert(None, {"id": 1, "name": "ada", "email": "ada@example.com"})
    assert user.name == "ada"


def test_update_email_returns_the_new_value():
    assert update_email(None, 1, "x@example.com")["email"] == "x@example.com"
''',
    "tests/test_serialisers.py": '''from app.serialisers import error_json, user_json


def test_user_json_keys():
    out = user_json({"id": 1, "name": "ada"})
    assert set(out) == {"id", "name", "email", "handle"}


def test_error_json_is_not_ok():
    assert error_json(["nope"])["ok"] is False
''',
    "tests/test_events.py": '''from services.events import accepting_submissions, current_phase


def test_phase_before_opening():
    assert current_phase({"opens_at": 10}, now=1) == "registration"


def test_not_accepting_after_close():
    event = {"opens_at": 0, "closes_at": 5, "judging_ends_at": 9}
    assert not accepting_submissions(event, now=7)
''',
    "tests/test_leaderboard_service.py": '''from services.leaderboard_service import board, position_of


def test_board_orders_by_total():
    rows = board([{"team": "a", "total": 1}, {"team": "b", "total": 9}],
                 {"idea": 1})
    assert rows[0]["team"] == "b"


def test_position_of_missing_team():
    assert position_of([{"team": "a"}], "zzz") is None
''',
    "tests/test_importer.py": '''from services.importer import parse_row, validate_rows


def test_email_is_normalised():
    assert parse_row({"email": " ADA@Example.com "})["email"] == "ada@example.com"


def test_bad_row_is_reported():
    assert validate_rows([{"id": 1, "email": "nope"}])[1]
''',
    "migrations/0001_create_users.sql": (
        "CREATE TABLE users (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  name TEXT NOT NULL,\n"
        "  email TEXT,\n"
        "  handle TEXT,\n"
        "  role TEXT NOT NULL,\n"
        "  digest INTEGER NOT NULL,\n"
        "  created_at TIMESTAMP\n"
        ");\n\n"
        "CREATE TABLE team_members (\n"
        "  team INTEGER NOT NULL,\n"
        "  user INTEGER NOT NULL\n"
        ");\n\n"
        "CREATE TABLE sessions (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  user INTEGER NOT NULL,\n"
        "  at TIMESTAMP\n"
        ");\n\n"
        "CREATE TABLE teams (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  owner INTEGER NOT NULL,\n"
        "  name TEXT NOT NULL\n"
        ");\n\n"
        "CREATE TABLE submissions (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  team INTEGER NOT NULL,\n"
        "  repo TEXT,\n"
        "  disqualified INTEGER\n"
        ");\n\n"
        "CREATE TABLE assignments (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  submission INTEGER NOT NULL,\n"
        "  judge INTEGER NOT NULL,\n"
        "  score NUMERIC\n"
        ");\n\n"
        "CREATE TABLE audit (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  actor INTEGER,\n"
        "  action TEXT NOT NULL,\n"
        "  subject TEXT,\n"
        "  detail TEXT\n"
        ");\n"
    ),
})

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
    {"agent": "ada", "tool": "Claude Code",
     "task": "Make email required during signup",
     "note": ("app/models.py",
              "every caller of create_user omits email today, so the default "
              "cannot just be dropped without fixing all of them")},
    {"agent": "grace", "tool": "Cursor",
     "task": "Redesign the signup form",
     "note": ("api/signup.py",
              "signup() builds the user directly rather than going through "
              "the auth path, so changes here bypass login()")},
    {"agent": "linus", "tool": "Codex",
     "task": "Tidy up the user table",
     "note": ("app/models.py",
              "to_dict is what sessions store, so dropping a field here "
              "silently changes what every logged-in request sees")},
    {"agent": "priya", "tool": "ChatGPT",
     "task": "Add a judging dashboard",
     "note": ("services/rubric.py",
              "weights are normalised on read, not on write, so a stored "
              "rubric that does not sum to one still scores")},
    {"agent": "sam", "tool": "Copilot",
     "task": "Pay out prizes automatically",
     "note": ("services/payouts.py",
              "prepare() marks a winner uncontactable rather than raising, so "
              "anything downstream has to check the flag")},
    # deliberately the same file grace is working in: one agent's finding
    # reaching another is the thing the context tab exists to show
    {"agent": "noor", "tool": "Gemini CLI",
     "task": "Let people fix their own details",
     "note": ("api/signup.py",
              "validators.check_signup returns every problem at once, so the "
              "form should render a list rather than the first error")},
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

    for person in ("ada", "grace", "linus", "priya", "sam", "noor"):
        store_mod.add_member(db, person, github=person)

    forecasts = []
    for entry in SEED_SESSIONS:
        agent, task = entry["agent"], entry["task"]
        store_mod.join_session(db, store_mod.session_id(root, agent),
                               repo["sha"], agent, task, entry.get("tool") or "mcp")
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

    # Brief everyone again now that the notes exist, so the findings actually
    # reach the other agents rather than sitting unread. Twice more, because an
    # agent asks repeatedly across a session — which is the whole reason the
    # shared half is worth caching, and the reason the numbers on the context
    # tab are worth looking at.
    found = risks(repo, forecasts)
    for _ in range(2):
        for entry, forecast in zip(SEED_SESSIONS, forecasts):
            brief(repo, [forecast], found, db=db, agent=entry["agent"],
                  store=store_mod)

    work = in_flight(repo, "main", db, store_mod)
    for note in observations(repo, work):
        if not store_mod.note_exists(db, note["file"], note["note"]):
            store_mod.add_note(db, repo["sha"], note["agent"], note["file"],
                               note["note"])
            store_mod.log(db, repo["sha"], "shared", "prophecy",
                          f"noted about {note['file']}: {note['note']}")
    return {"sessions": len(SEED_SESSIONS), "people": 3}
