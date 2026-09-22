"""Who is asking, and what they are allowed to do.

Prophecy started as something you run on your own machine, where the answer
to "who is this" is "you". The moment it is reachable by a team it needs a
real answer, because the endpoints that matter write files, commit them and
fetch repositories.

Identity comes from GitHub rather than from a password table here. Nobody
wants another password, GitHub is already where the repositories and the
collaborators are, and an organisation there is exactly the group this wants
to talk about. So membership of your org is the thing that grants access,
and an explicit list of people is the thing that overrides it.

Three roles, deliberately few:

    owner   manages who gets in, and everything a member can do
    member  reads, and writes: the editor, commits, reverts, fetching a repo
    viewer  reads

Everything here is stdlib. Sessions are random tokens stored hashed, so the
database is not a list of live credentials; the cookie is the only copy.
"""

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"

# Long enough not to be a nuisance, short enough that a forgotten laptop
# stops being a way in within the week.
SESSION_DAYS = 7
STATE_SECONDS = 600
HTTP_TIMEOUT = 15

ROLES = ("owner", "member", "viewer")
WRITER_ROLES = ("owner", "member")


def secret(repo_path):
    """The key everything here is signed with.

    From the environment when it is set, so a container or a systemd unit can
    supply it and sessions survive a restart. Otherwise generated once and
    kept next to the database, readable only by the account running this.
    """
    from_env = os.environ.get("PROPHECY_SECRET", "").strip()
    if from_env:
        return from_env.encode()

    path = Path(repo_path) / ".prophecy" / "secret"
    if path.exists():
        return path.read_bytes().strip()

    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32).encode()
    # written before the mode is set, so it is created by us and narrowed
    # immediately rather than existing world-readable for any length of time
    path.touch(mode=0o600, exist_ok=True)
    path.write_bytes(value)
    os.chmod(path, 0o600)
    return value


def _sign(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def hash_token(token):
    """What goes in the database. The token itself never does."""
    return hashlib.sha256(token.encode()).hexdigest()


def make_state(key):
    """A signed, expiring nonce, so a callback has to come from a login.

    Without it anyone can send someone a callback URL of their choosing and
    have the browser act on it.
    """
    stamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(16)
    body = f"{stamp}.{nonce}"
    return f"{body}.{_sign(key, body)}"


def check_state(key, state):
    try:
        stamp, nonce, signature = (state or "").split(".", 2)
    except ValueError:
        return False
    body = f"{stamp}.{nonce}"
    if not hmac.compare_digest(signature, _sign(key, body)):
        return False
    try:
        age = time.time() - int(stamp)
    except ValueError:
        return False
    return 0 <= age <= STATE_SECONDS


def csrf_token(key, session_token):
    """Derived from the session, so it needs the cookie to be computed.

    A cookie is sent by the browser whether or not the page asked for it,
    which is the whole trick behind cross-site requests. A header that only
    our own page can fill in is what makes a write deliberate.
    """
    return _sign(key, "csrf:" + session_token)


def authorize_url(client_id, redirect_uri, state, want_orgs=True):
    scope = "read:user"
    if want_orgs:
        # to see which organisations someone belongs to, including the
        # private membership that most company accounts use
        scope += " read:org"
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "allow_signup": "false",
    })
    return f"{GITHUB_AUTHORIZE}?{query}"


def _post_json(url, data, headers=None):
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read() or b"{}")


def _get_json(url, token):
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("User-Agent", "prophecy")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read() or b"{}")


def exchange_code(client_id, client_secret, code, redirect_uri):
    """Turn the callback's code into a token, or say why it could not."""
    try:
        answer = _post_json(GITHUB_TOKEN, {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        })
    except (urllib.error.URLError, ValueError) as exc:
        return {"error": f"GitHub did not answer: {exc}"}
    if answer.get("error"):
        return {"error": answer.get("error_description") or answer["error"]}
    token = answer.get("access_token")
    if not token:
        return {"error": "GitHub returned no access token."}
    return {"token": token}


def identity(token):
    """The person, and the organisations they are in.

    Organisations are best-effort: a token without read:org still identifies
    somebody, they just cannot be let in by org membership alone.
    """
    try:
        user = _get_json(f"{GITHUB_API}/user", token)
    except (urllib.error.URLError, ValueError) as exc:
        return {"error": f"Could not read your GitHub account: {exc}"}

    orgs = []
    try:
        orgs = [o["login"].lower()
                for o in _get_json(f"{GITHUB_API}/user/orgs", token)]
    except (urllib.error.URLError, ValueError, TypeError, KeyError):
        pass

    teams = []
    try:
        teams = [f"{t['organization']['login']}/{t['slug']}".lower()
                 for t in _get_json(f"{GITHUB_API}/user/teams", token)]
    except (urllib.error.URLError, ValueError, TypeError, KeyError):
        pass

    return {
        "login": user.get("login", ""),
        "github_id": user.get("id"),
        "name": user.get("name") or user.get("login", ""),
        "avatar": user.get("avatar_url", ""),
        "orgs": orgs,
        "teams": teams,
    }


def role_for(person, rules, owner_login=""):
    """What this person may do here, given the rules on this instance.

    The most specific rule wins: a rule naming you beats one naming your
    team, which beats one naming your organisation. Otherwise somebody
    removed by name would still be let in by their org.
    """
    login = (person.get("login") or "").lower()
    if owner_login and login == owner_login.lower():
        return "owner"

    orgs = {o.lower() for o in person.get("orgs", [])}
    teams = {t.lower() for t in person.get("teams", [])}

    for kind, matches in (("user", {login}), ("team", teams), ("org", orgs)):
        best = None
        for rule in rules:
            if rule["kind"] != kind or rule["value"].lower() not in matches:
                continue
            if best is None or ROLES.index(rule["role"]) < ROLES.index(best):
                best = rule["role"]   # the most trusted rule of that kind
        if best:
            return best
    return ""


def may_write(role):
    return role in WRITER_ROLES


def new_session_token():
    return secrets.token_urlsafe(32)


def cookie_header(token, secure, max_age=None):
    """HttpOnly so script cannot read it, Lax so a cross-site POST cannot
    carry it, Secure whenever this is not plain local http."""
    age = SESSION_DAYS * 24 * 3600 if max_age is None else max_age
    parts = [
        f"prophecy_session={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={age}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def read_cookie(header):
    """The session token out of a Cookie header, without a cookie library."""
    for piece in (header or "").split(";"):
        name, _, value = piece.strip().partition("=")
        if name == "prophecy_session":
            return value.strip()
    return ""
