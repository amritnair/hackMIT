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
    ran_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(repo_path):
    directory = Path(repo_path).resolve() / ".mergemind"
    directory.mkdir(exist_ok=True)
    db = sqlite3.connect(directory / "mergemind.db")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
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


def save_outcome(db, repo, outcome, comparison):
    db.execute(
        "INSERT INTO outcomes (sha, base, branch, merged_clean, tests_passed, detail)"
        " VALUES (?,?,?,?,?,?)",
        (repo["sha"], outcome["base"], outcome["branch"],
         int(outcome["merged_clean"]),
         -1 if comparison["tests_passed"] is None else int(comparison["tests_passed"]),
         json.dumps(comparison)),
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
    ).fetchone()
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
        "recurring": repeats,
    }
    # Accuracy over three merges is noise dressed as a number.
    out["accuracy"] = (
        "not enough merge runs yet (need at least 10, have "
        f"{out['merges_run']})" if out["merges_run"] < 10 else "see outcomes table"
    )
    return out
