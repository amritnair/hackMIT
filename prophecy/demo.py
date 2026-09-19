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

    made = []
    for name, spec in BRANCHES.items():
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
