"""A SQLite file in .mergemind/ so a risk you saw yesterday can be looked up
today, and so forecasts can be compared against merges that happen later.

Deliberately dumb: rows in, rows out, JSON blobs for anything structured.
"""

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
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sha TEXT, base TEXT, branch TEXT,
    merged_clean INT, tests_passed INT, detail TEXT,
    source TEXT DEFAULT 'verify',
    ran_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(repo_path):
    directory = Path(repo_path).resolve() / ".mergemind"
    directory.mkdir(exist_ok=True)
    db = sqlite3.connect(directory / "mergemind.db")
    db.row_factory = sqlite3.Row
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
        f"{replayed} merge(s) replayed by backfill — run `mergemind backfill` "
        "for the scored breakdown."
        if replayed else
        "no history replayed yet. `mergemind backfill` grades the forecast "
        "against merges that already happened; nothing else here can."
    )
    return out
