"""A SQLite file in .prophecy/ so a risk you saw yesterday can be looked up
today, and so forecasts can be compared against merges that happen later.

Deliberately dumb: rows in, rows out, JSON blobs for anything structured.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    sha TEXT PRIMARY KEY, branch TEXT, seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
    file_count INT, symbol_count INT
);
CREATE TABLE IF NOT EXISTS risks (
    id TEXT PRIMARY KEY, sha TEXT, risk_type TEXT, risk_score REAL,
    risk_level TEXT, tasks TEXT, evidence TEXT, recommendation TEXT,
    confidence REAL, first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sha TEXT, agent TEXT, task TEXT,
    prefix_hash TEXT, prefix_tokens INT, suffix_tokens INT,
    baseline_tokens INT, notes_pulled INT DEFAULT 0,
    issued_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sha TEXT, agent TEXT, file TEXT,
    note TEXT, written_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, sha TEXT, agent TEXT, task TEXT, client TEXT,
    joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT DEFAULT CURRENT_TIMESTAMP, left_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sha TEXT, kind TEXT, agent TEXT,
    detail TEXT, tokens INT DEFAULT 0, at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, github TEXT,
    role TEXT, added_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(name)
);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sha TEXT, label TEXT, agent TEXT,
    head_sha TEXT, risk_score INT, risk_band TEXT, confidence REAL,
    files TEXT, failures TEXT, said TEXT, intent TEXT,
    at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sha TEXT, base TEXT, branch TEXT,
    merged_clean INT, tests_passed INT, detail TEXT,
    source TEXT DEFAULT 'verify',
    ran_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sha TEXT, to_agent TEXT,
    subject TEXT, body TEXT, sent_by TEXT DEFAULT 'dashboard',
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP, delivered_at TEXT
);
"""


def connect(repo_path):
    directory = Path(repo_path).resolve() / ".prophecy"
    directory.mkdir(exist_ok=True)
    db = sqlite3.connect(directory / "prophecy.db")
    db.row_factory = sqlite3.Row
    # Several agents work in one project at once, each in its own process.
    # Without a wait, whichever one loses a write race sees "database is
    # locked" and reports a failure that is really just a queue.
    db.execute("PRAGMA busy_timeout = 5000")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    try:  # databases written before outcomes knew where they came from
        db.execute("ALTER TABLE outcomes ADD COLUMN source TEXT DEFAULT 'verify'")
    except sqlite3.OperationalError:
        pass
    return db


def save_snapshot(db, repo):
    db.execute(
        "INSERT OR REPLACE INTO snapshots (sha, branch, file_count, symbol_count)"
        " VALUES (?,?,?,?)",
        (repo["sha"], repo["branch"], len(repo["files"]),
         sum(len(f["symbols"]) for f in repo["files"].values())),
    )
    db.commit()


def save_risks(db, found):
    for r in found:
        db.execute(
            "INSERT INTO risks (id, sha, risk_type, risk_score, risk_level, tasks,"
            " evidence, recommendation, confidence) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP,"
            " risk_score=excluded.risk_score, risk_level=excluded.risk_level",
            (r["id"], r["repository_sha"], r["risk_type"], r["risk_score"],
             r["risk_level"], json.dumps(r["tasks"]), json.dumps(r["evidence"]),
             r["recommendation"], r["confidence"]),
        )
    db.commit()


def save_outcome(db, repo, outcome, comparison, source="verify"):
    db.execute(
        "INSERT INTO outcomes (sha, base, branch, merged_clean, tests_passed,"
        " detail, source) VALUES (?,?,?,?,?,?,?)",
        (repo["sha"], outcome["base"], outcome["branch"],
         int(outcome["merged_clean"]),
         -1 if comparison["tests_passed"] is None else int(comparison["tests_passed"]),
         json.dumps(comparison), source),
    )
    db.commit()


def record_brief(db, sha, agent, task, prefix_hash, prefix_tokens,
                 suffix_tokens, baseline, notes_pulled=0):
    db.execute(
        "INSERT INTO briefs (sha, agent, task, prefix_hash, prefix_tokens,"
        " suffix_tokens, baseline_tokens, notes_pulled) VALUES (?,?,?,?,?,?,?,?)",
        (sha, agent, task, prefix_hash, prefix_tokens, suffix_tokens,
         baseline, notes_pulled),
    )
    db.commit()


def add_note(db, sha, agent, file, note):
    """One agent's finding about one file, for the next agent who goes there.

    Re-sharing the same finding is a no-op. An agent that reconnects and says
    the same thing again should not make the next person read it twice.
    """
    already = db.execute(
        "SELECT 1 FROM notes WHERE file = ? AND agent = ? AND note = ? LIMIT 1",
        (file, agent, note),
    ).fetchone()
    if already:
        return False
    db.execute(
        "INSERT INTO notes (sha, agent, file, note) VALUES (?,?,?,?)",
        (sha, agent, file, note),
    )
    db.commit()
    return True


def note_exists(db, file, note):
    """Observations are re-derived on every run; writing them again each time
    would turn the shared context into the same sentence forty times."""
    return db.execute(
        "SELECT 1 FROM notes WHERE file = ? AND note = ? LIMIT 1", (file, note)
    ).fetchone() is not None


def notes_for(db, files, agent=None, limit=12):
    """Notes other agents left about these files, newest first.

    Excludes the asking agent's own notes: an agent does not need to be told
    what it already worked out, and re-reading its own findings is exactly the
    duplicated context this is meant to remove.
    """
    if not files:
        return []
    marks = ",".join("?" * len(files))
    sql = f"SELECT * FROM notes WHERE file IN ({marks})"
    params = list(files)
    if agent:
        sql += " AND agent <> ?"
        params.append(agent)
    sql += " ORDER BY written_at DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in db.execute(sql, params)]


# Anthropic list price for input tokens, dollars per million. The saving is
# reported in money because tokens are not a unit anyone budgets in.
PRICE_PER_MTOK = 5.00


def usage(db):
    """How the agents actually used this, and what the sharing bought.

    Prefix reuse is measured by identical prefix bytes on the same commit. It
    is an upper bound on what a cache could have served: whether the provider
    really had it warm depends on the TTL, which nothing here can see.
    """
    rows = [dict(r) for r in db.execute("SELECT * FROM briefs ORDER BY issued_at")]
    if not rows:
        return {"briefs": 0, "note": "No briefs issued yet."}

    seen, first_time, reused = set(), 0, 0
    sent_actual, sent_naive = 0, 0
    for row in rows:
        key = (row["sha"], row["prefix_hash"])
        if key in seen:
            reused += 1
            sent_actual += row["suffix_tokens"]  # prefix served from cache
        else:
            seen.add(key)
            first_time += 1
            sent_actual += row["prefix_tokens"] + row["suffix_tokens"]
        sent_naive += (row["baseline_tokens"] or 0) + row["suffix_tokens"]

    per_agent = [dict(r) for r in db.execute(
        "SELECT agent, COUNT(*) briefs, SUM(suffix_tokens) task_tokens,"
        " SUM(notes_pulled) notes_pulled FROM briefs GROUP BY agent"
        " ORDER BY briefs DESC"
    )]
    shared = [dict(r) for r in db.execute(
        "SELECT file, COUNT(*) n, COUNT(DISTINCT agent) agents FROM notes"
        " GROUP BY file HAVING agents > 1 ORDER BY agents DESC, n DESC LIMIT 8"
    )]
    return {
        "briefs": len(rows),
        "agents": len(per_agent),
        "prefix_first_time": first_time,
        "prefix_reused": reused,
        "reuse_rate": round(reused / len(rows), 2),
        "tokens_sent": sent_actual,
        "tokens_if_each_agent_read_the_repo": sent_naive,
        "tokens_avoided": sent_naive - sent_actual,
        "dollars_avoided": round((sent_naive - sent_actual) / 1e6 * PRICE_PER_MTOK, 2),
        "dollars_spent": round(sent_actual / 1e6 * PRICE_PER_MTOK, 2),
        "notes_written": db.execute("SELECT COUNT(*) n FROM notes").fetchone()["n"],
        "notes_pulled": sum(r["notes_pulled"] or 0 for r in per_agent),
        "per_agent": per_agent,
        "shared_files": shared,
        "note": "Reuse is counted by identical prefix bytes on one commit — "
                "what a cache could serve, not confirmation that it did.",
    }


def add_member(db, name, github="", role=""):
    db.execute(
        "INSERT INTO members (name, github, role) VALUES (?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET github=excluded.github,"
        " role=excluded.role",
        (name, github, role),
    )
    db.commit()


def remove_member(db, name):
    db.execute("DELETE FROM members WHERE name = ?", (name,))
    db.commit()


def members(db):
    return [dict(r) for r in db.execute("SELECT * FROM members ORDER BY name")]


def whose(db, handle):
    """Map a git author or GitHub login onto someone on the project."""
    row = db.execute(
        "SELECT name FROM members WHERE github = ? OR name = ? LIMIT 1",
        (handle, handle),
    ).fetchone()
    return row["name"] if row else handle


def save_verdict(db, repo_sha, analysis):
    """Keep what we said about a change, so it can be read back later.

    A prediction nobody can look up afterwards is not accountable. This is the
    row the track record reads, and the row a postmortem would want.
    """
    db.execute(
        "INSERT INTO verdicts (sha, label, agent, head_sha, risk_score,"
        " risk_band, confidence, files, failures, said, intent)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (repo_sha, analysis["label"], analysis.get("agent") or "",
         analysis.get("head_sha") or "", analysis["risk_score"],
         analysis["risk_band"], analysis["confidence"],
         json.dumps(analysis["changed_files"]),
         json.dumps([{k: f[k] for k in ("severity", "title", "detail")}
                     for f in analysis["potential_failures"]]),
         json.dumps(analysis["recommendations"]),
         json.dumps(analysis.get("intent", {}))),
    )
    db.commit()


def verdicts(db, limit=50):
    out = []
    for row in db.execute("SELECT * FROM verdicts ORDER BY id DESC LIMIT ?",
                          (limit,)):
        item = dict(row)
        for field in ("files", "failures", "said", "intent"):
            try:
                item[field] = json.loads(item[field] or "null")
            except (TypeError, ValueError):
                item[field] = None
        out.append(item)
    return out


def log(db, sha, kind, agent, detail, tokens=0):
    """One line in the feed. The feed is the demo and the audit trail both."""
    db.execute(
        "INSERT INTO events (sha, kind, agent, detail, tokens) VALUES (?,?,?,?,?)",
        (sha, kind, agent, detail, tokens),
    )
    db.commit()


def session_id(repo_path, agent):
    """Stable for one agent in one project, whoever is asking.

    Both the MCP server and anything that seeds sessions derive the id the
    same way; when they did not, one agent reconnecting showed up twice.
    """
    seed = f"{Path(repo_path).resolve()}|{agent}".encode()
    return hashlib.blake2s(seed, digest_size=8).hexdigest()


def join_session(db, session_id, sha, agent, task, client=""):
    db.execute(
        "INSERT INTO sessions (id, sha, agent, task, client) VALUES (?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET task=excluded.task,"
        " last_seen=CURRENT_TIMESTAMP, left_at=NULL",
        (session_id, sha, agent, task, client),
    )
    db.commit()


def touch_session(db, session_id):
    db.execute("UPDATE sessions SET last_seen=CURRENT_TIMESTAMP WHERE id=?",
               (session_id,))
    db.commit()


def leave_session(db, session_id):
    db.execute("UPDATE sessions SET left_at=CURRENT_TIMESTAMP WHERE id=?",
               (session_id,))
    db.commit()


def live_sessions(db, minutes=30):
    """Sessions that have not left and were heard from recently.

    A crashed agent never says goodbye, so anything quiet for longer than the
    window is treated as gone rather than left hanging in the list.
    """
    return [dict(r) for r in db.execute(
        "SELECT * FROM sessions WHERE left_at IS NULL"
        f" AND last_seen > datetime('now', '-{int(minutes)} minutes')"
        " ORDER BY joined_at"
    )]


def feed(db, limit=40):
    return [dict(r) for r in db.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    )]


def sharing(db, limit=120):
    """The context one agent handed to the next, as a shape rather than a total.

    The money on the Context tab answers "what did the sharing save". This
    answers the question underneath it: what was actually shared, by whom, and
    where two agents ended up reading the same thing.
    """
    notes = [dict(r) for r in db.execute(
        "SELECT agent, file, note, written_at FROM notes"
        " ORDER BY written_at DESC LIMIT ?", (limit,)
    )]
    files = [dict(r) for r in db.execute(
        "SELECT file, COUNT(*) notes, COUNT(DISTINCT agent) agents"
        " FROM notes GROUP BY file ORDER BY agents DESC, notes DESC"
    )]
    agents = [dict(r) for r in db.execute(
        "SELECT agent, COUNT(*) wrote, COUNT(DISTINCT file) files"
        " FROM notes GROUP BY agent ORDER BY wrote DESC"
    )]
    briefed = {r["agent"]: dict(r) for r in db.execute(
        "SELECT agent, COUNT(*) briefs, SUM(suffix_tokens) task_tokens,"
        " SUM(notes_pulled) pulled FROM briefs GROUP BY agent"
    )}
    for row in agents:
        row.update(briefed.pop(row["agent"], {}))
    # an agent can have been briefed without having found anything worth saying
    for left in briefed.values():
        left["wrote"], left["files"] = 0, 0
        agents.append(left)

    rows = [dict(r) for r in db.execute("SELECT * FROM briefs")]
    seen, reused, prefix_tokens = set(), 0, 0
    for row in rows:
        key = (row["sha"], row["prefix_hash"])
        if key in seen:
            reused += 1
        else:
            seen.add(key)
            prefix_tokens += row["prefix_tokens"]
    return {
        "notes": notes,
        "files": files,
        "agents": sorted(agents, key=lambda a: -(a.get("wrote") or 0)),
        "shared_background": {
            "briefs": len(rows),
            "paid_once_tokens": prefix_tokens,
            "served_from_cache": reused,
        },
        "live": [s["agent"] for s in live_sessions(db)],
    }


def queue_message(db, sha, to_agent, subject, body, sent_by="dashboard"):
    """Something for one agent to read the next time it calls in.

    An agent is not a server: it cannot be pushed to, it can only be answered.
    So a risk profile addressed to an agent waits here until that agent's next
    MCP call, and rides back on the reply.

    Sending the same unread thing twice is a no-op, the same way re-sharing a
    finding is. Pressing the button again while the agent has not called in yet
    should not make it read the same profile twice.
    """
    already = db.execute(
        "SELECT id FROM messages WHERE to_agent = ? AND subject = ?"
        " AND body = ? AND delivered_at IS NULL LIMIT 1",
        (to_agent, subject, body),
    ).fetchone()
    if already:
        return already["id"]
    cur = db.execute(
        "INSERT INTO messages (sha, to_agent, subject, body, sent_by)"
        " VALUES (?,?,?,?,?)",
        (sha, to_agent, subject, body, sent_by),
    )
    db.commit()
    return cur.lastrowid


def pending_messages(db, agent, mark=True):
    """Undelivered messages for one agent, marked delivered as they go out."""
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM messages WHERE to_agent = ? AND delivered_at IS NULL"
        " ORDER BY id", (agent,)
    )]
    if rows and mark:
        db.execute(
            "UPDATE messages SET delivered_at = CURRENT_TIMESTAMP"
            " WHERE id IN (%s)" % ",".join("?" * len(rows)),
            [r["id"] for r in rows],
        )
        db.commit()
    return rows


def messages(db, limit=40):
    """Everything sent to an agent, delivered or still waiting."""
    return [dict(r) for r in db.execute(
        "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
    )]


def get_risk(db, risk_id):
    row = db.execute("SELECT * FROM risks WHERE id = ?", (risk_id,)).fetchone()
    if not row:
        return None
    risk = dict(row)
    risk["tasks"] = json.loads(risk["tasks"])
    risk["evidence"] = json.loads(risk["evidence"])
    return risk


def insights(db):
    """History, with enough honesty to be useless when it should be."""
    counts = {
        row["risk_type"]: row["n"] for row in db.execute(
            "SELECT risk_type, COUNT(*) n FROM risks GROUP BY risk_type ORDER BY n DESC"
        )
    }
    merges = db.execute(
        "SELECT COUNT(*) n, SUM(merged_clean) clean FROM outcomes"
        " WHERE source = 'verify'"
    ).fetchone()
    replayed = db.execute(
        "SELECT COUNT(*) n FROM outcomes WHERE source = 'backfill'"
    ).fetchone()["n"]
    repeats = [
        dict(row) for row in db.execute(
            "SELECT id, risk_type, tasks, first_seen, last_seen FROM risks"
            " WHERE first_seen <> last_seen ORDER BY last_seen DESC LIMIT 5"
        )
    ]
    out = {
        "snapshots": db.execute("SELECT COUNT(*) n FROM snapshots").fetchone()["n"],
        "risks_recorded": sum(counts.values()),
        "by_type": counts,
        "merges_run": merges["n"] or 0,
        "merged_clean": merges["clean"] or 0,
        "merges_replayed": replayed,
        "recurring": repeats,
    }
    # Accuracy over three merges is noise dressed as a number. The real
    # figures come from replaying history, not from the handful of merges
    # somebody happened to run by hand.
    out["accuracy"] = (
        f"{replayed} merge(s) replayed by backfill — run `prophecy backfill` "
        "for the scored breakdown."
        if replayed else
        "no history replayed yet. `prophecy backfill` grades the forecast "
        "against merges that already happened; nothing else here can."
    )
    return out
