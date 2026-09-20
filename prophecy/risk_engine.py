"""Change → Context → Consequences.

The question is not "is there a bug in this diff". It is "given everything
else happening in this repository, what is this change likely to break".

Everything here is built from evidence that was computed deterministically —
the dependency graph, signature-level diffs, git history, and the other work
in flight. A model is never asked to invent facts; it is only ever asked to
interpret ones already established, and it is optional even for that.

Scores are ranges. A single number would be false precision: the analysis
knows how many files import a symbol, and does not know how often any of them
runs. The gap between those two is exactly what the range is for.
"""

import re
from collections import Counter
from pathlib import Path

from .branches import diff
from .scan import CODE_SUFFIXES, git, is_regenerated

BANDS = ((80, "critical"), (60, "high"), (35, "medium"), (0, "low"))

# Words that, in a path or symbol, suggest the code sits on a boundary where
# mistakes are expensive. Never used on their own — a match only counts when
# the thing it names is also depended on by something. Hard-coding "auth = 100"
# would make the model unexplainable and usually wrong.
SENSITIVE = {
    "auth": "authentication", "login": "authentication", "session": "sessions",
    "token": "credentials", "password": "credentials", "secret": "credentials",
    "credential": "credentials", "permission": "authorization",
    "role": "authorization", "payment": "payments", "billing": "payments",
    "charge": "payments", "invoice": "payments", "migration": "data",
    "schema": "data", "model": "data", "persist": "data",
}


def band(score):
    return next(name for cutoff, name in BANDS if score >= cutoff)


def blast_radius(repo, files, depth=3):
    """Who reaches this change, directly and then transitively.

    Returns each layer separately: a direct caller is stronger evidence than
    something three hops away, and collapsing them into one number throws that
    distinction away.
    """
    layers, seen, frontier = [], set(files), set(files)
    for _ in range(depth):
        nxt = set()
        for path in frontier:
            for caller in repo["callers"].get(path, []):
                if caller not in seen:
                    nxt.add(caller)
        if not nxt:
            break
        layers.append(sorted(nxt))
        seen |= nxt
        frontier = nxt
    return layers


def churn(repo_path, path, commits=200):
    """How often this file has been changing lately.

    Historical instability is evidence, not proof: a file that changes weekly
    is more likely to be changing under someone else right now.
    """
    try:
        log = git(repo_path, "log", f"-{commits}", "--format=%H", "--", path)
    except Exception:
        return 0
    return len([line for line in log.splitlines() if line.strip()])


def criticality(repo, path, layers):
    """An explainable estimate of what it costs to get this file wrong."""
    signals, score = [], 0
    direct = repo["callers"].get(path, [])
    reach = sum(len(layer) for layer in layers)

    if direct:
        score += min(len(direct) * 4, 24)
        signals.append(f"{len(direct)} file(s) import it directly")
    if reach > len(direct):
        score += min((reach - len(direct)) * 2, 16)
        signals.append(f"{reach} file(s) reach it once indirect imports are followed")

    # spread across top-level packages: something used by one package is a
    # local concern, something used by four is an architectural one
    packages = {Path(c).parts[0] for c in direct if Path(c).parts}
    if len(packages) > 1:
        score += min(len(packages) * 4, 16)
        signals.append(
            f"used from {len(packages)} separate top-level areas: "
            + ", ".join(sorted(packages)[:4])
        )

    if path in repo.get("schema_files", []):
        score += 18
        signals.append("it defines or migrates stored data, which outlives a deploy")
    if path in repo.get("manifests", []):
        score += 8
        signals.append("it pins dependencies for everything else")

    low = path.lower()
    hits = {label for word, label in SENSITIVE.items() if word in low}
    if hits and direct:
        score += min(len(hits) * 7, 18)
        signals.append(
            f"it sits on a {', '.join(sorted(hits))} path and other code depends on it"
        )
    elif hits:
        signals.append(
            f"it names {', '.join(sorted(hits))}, but nothing here imports it, "
            "so the blast radius looks small"
        )

    return min(score, 100), signals


def _params(signature):
    inside = re.search(r"\((.*)\)", signature or "")
    if not inside:
        return []
    return [p.strip() for p in inside.group(1).split(",") if p.strip()]


def potential_failures(repo, change, layers):
    """Concrete ways this change could break something, with the evidence.

    Only things that can be pointed at. No style opinions, no "consider
    refactoring" — every entry names a file that will have to change or a
    record that will not fit.
    """
    found = []
    for sig in change["signature_changes"]:
        path, symbol = sig["file"], sig["symbol"]
        callers = repo["callers"].get(path, [])
        before, after = _params(sig["before"]), _params(sig["after"])
        added = [p for p in after if p not in before and "=" not in p
                 and not p.startswith("*")]
        # optional -> required: the name is unchanged, the default is gone
        now_required = [p for p in after
                        if "=" not in p and f"{p}=..." in before]
        added = [p for p in added if p not in now_required]

        if sig.get("kind") == "moved":
            if callers:
                found.append({
                    "severity": "high",
                    "title": f"{symbol} moved out of {path}",
                    "detail": (
                        f"{len(callers)} file(s) import {path} and will need to "
                        f"import from {sig['moved_to']} instead."
                    ),
                    "affected": callers[:8],
                })
        elif sig["after"] == "(removed)":
            found.append({
                "severity": "critical" if callers else "low",
                "title": f"{symbol} no longer exists in {path}",
                "detail": (
                    f"{len(callers)} file(s) import this module."
                    if callers else
                    "Nothing in this repository imports it, so the reach looks small."
                ),
                "affected": callers[:8],
            })
        elif now_required:
            found.append({
                "severity": "critical" if len(callers) > 2 else "high",
                "title": f"{symbol} no longer accepts a missing "
                         f"{', '.join(now_required)}",
                "detail": (
                    f"{', '.join(now_required)} used to have a default. Every "
                    f"call that relied on it has to pass one now, and "
                    f"{len(callers)} file(s) import {path}."
                    if callers else
                    f"{', '.join(now_required)} used to have a default."
                ),
                "affected": callers[:8],
            })
        elif added and callers:
            found.append({
                "severity": "critical" if len(callers) > 4 else "high",
                "title": f"{symbol} now requires {', '.join(added)}",
                "detail": (
                    f"Every existing call has to pass it. {len(callers)} file(s) "
                    f"import {path}."
                ),
                "affected": callers[:8],
            })
        elif added:
            found.append({
                "severity": "medium",
                "title": f"{symbol} now requires {', '.join(added)}",
                "detail": "No importers found here, so existing callers may be "
                          "outside this repository.",
                "affected": [],
            })

    changed = set(change["changed_files"])
    # Only real DDL counts as "stored data" here. A file called models.py is
    # both the schema and the code that reads it, and treating it as schema
    # alone produces the nonsense claim that code was not updated while
    # pointing at the code that was.
    schema = [f for f in changed if f in repo.get("schema_files", [])
              and Path(f).suffix not in CODE_SUFFIXES]
    code = [f for f in changed if Path(f).suffix in CODE_SUFFIXES]
    if schema and not code:
        found.append({
            "severity": "high",
            "title": "Stored data changes, application code does not",
            "detail": (
                f"{', '.join(schema[:3])} changes what is stored, but no "
                "application file in this change was updated to match. "
                "Existing rows and existing readers still follow the old shape."
            ),
            "affected": schema,
        })
    if code and schema:
        found.append({
            "severity": "medium",
            "title": "Stored data and code are changing together",
            "detail": ("Ordering matters: whichever lands first has to tolerate "
                       "the other side being old."),
            "affected": schema + code[:4],
        })

    tests_changed = [f for f in changed if repo["files"].get(f, {}).get("is_test")]
    touched_tests = {
        t for t, info in repo["files"].items()
        if info["is_test"] and set(info["imports"]) & {
            Path(c).stem for c in changed}
    }
    if touched_tests and not tests_changed:
        found.append({
            "severity": "medium",
            "title": "Tests cover this code and were not touched",
            "detail": (
                f"{len(touched_tests)} test file(s) import what is changing. "
                "Either they still pass and the behaviour really is unchanged, "
                "or they encode the old behaviour."
            ),
            "affected": sorted(touched_tests)[:6],
        })

    for path in changed:
        if is_regenerated(path):
            found.append({
                "severity": "low",
                "title": f"{path} is generated or high-churn",
                "detail": "Worth regenerating rather than merging by hand.",
                "affected": [path],
            })
    return found


def infer_intent(repo_path, base, head, label=""):
    """What the author appears to be trying to do, from what they wrote down."""
    try:
        subjects = git(repo_path, "log", "--format=%s", f"{base}..{head}").splitlines()
    except Exception:
        subjects = []
    text = " ".join([label, *subjects]).strip()
    return {"text": text or "(no commit messages)", "commits": subjects[:6]}


def _tightens(failures):
    """Did this change make something stricter than it was?"""
    return any("now requires" in f["title"] or "no longer accepts" in f["title"]
               for f in failures)


CONTRADICTIONS = [
    (r"\boptional\b|\bnullable\b|\bnot required\b|\ballow(?:s|ing)? (?:null|empty)\b",
     _tightens,
     "The message describes making something optional or allowing it to be "
     "missing, but the change makes a parameter required."),
    (r"\bremove|\bdelete|\bdrop\b|\bdeprecat",
     _tightens,
     "The message describes removing something, but the change makes a "
     "parameter required rather than removing it."),
    (r"\brename\b",
     lambda f: any("no longer exists" in x["title"] for x in f),
     "The message describes a rename. Anything still importing the old name "
     "will not find it."),
    (r"\bno.?op\b|\brefactor\b|\bcleanup\b|\btidy\b|\bformat",
     lambda f: any(x["severity"] in ("high", "critical") for x in f),
     "The message describes a change with no behavioural effect, but the "
     "change alters an interface other code depends on."),
]


def check_intent(intent, failures):
    """Does the implementation look like what the message says it is?"""
    text = (intent.get("text") or "").lower()
    out = []
    for pattern, test, explanation in CONTRADICTIONS:
        if re.search(pattern, text) and test(failures):
            out.append(explanation)
    return out


SEVERITY_WEIGHT = {"critical": 34, "high": 20, "medium": 9, "low": 2}


def score_change(repo, change, failures, crit, concurrent_overlap):
    """A range, not a number, and the reasons the range is that wide."""
    base = sum(SEVERITY_WEIGHT.get(f["severity"], 0) for f in failures)
    base = min(base, 70)
    base += min(crit * 0.25, 22)
    if concurrent_overlap:
        base += min(len(concurrent_overlap) * 6, 18)

    unknowns = []
    weak = [f for f in change["changed_files"]
            if Path(f).suffix in {".ts", ".tsx", ".js", ".jsx"}]
    if weak:
        unknowns.append(
            f"{len(weak)} changed file(s) are TypeScript or JavaScript, read "
            "with pattern matching rather than a parser, so references may be "
            "missed"
        )
    unknowns.append(
        "nothing here observes runtime, so how often the affected code "
        "actually runs is unknown"
    )
    if any(f["affected"] == [] for f in failures):
        unknowns.append("some affected callers may live outside this repository")

    spread = 6 + 4 * len(unknowns)
    score = round(min(base, 97))
    low = max(0, score - spread)
    high = min(100, score + spread)
    # confidence falls as the unknowns pile up, and rises with hard evidence
    evidence_count = sum(len(f["affected"]) for f in failures)
    confidence = round(min(0.92, 0.45 + 0.05 * evidence_count - 0.06 * len(unknowns)), 2)
    return {
        "risk_score": score,
        "risk_range": {"min": low, "max": high},
        "risk_band": band(score),
        "criticality_band": band(crit),
        "confidence": max(confidence, 0.25),
        "uncertainty": unknowns,
    }


def analyze_change(repo, base, head, label="", agent=None, task=None,
                   concurrent=(), stated_intent=""):
    """The whole loop for one change, as structured data."""
    repo_path = repo["repo"]
    try:
        fork = git(repo_path, "merge-base", base, head)
    except Exception:
        fork = base
    change = diff(repo_path, fork, head, label=label or head)
    changed = change["changed_files"]

    layers = blast_radius(repo, changed)
    failures = potential_failures(repo, change, layers)
    intent = infer_intent(repo_path, fork, head, label)
    if stated_intent:
        # What the agent says it is doing counts as intent too, and is often
        # the only place the intent is written down before the commit exists.
        intent["stated"] = stated_intent
        intent["text"] = f"{stated_intent}. {intent['text']}"
    contradictions = check_intent(intent, failures)

    crits = [criticality(repo, f, blast_radius(repo, [f])) for f in changed
             if f in repo["files"]]
    crit = max((c for c, _ in crits), default=0)
    crit_signals = [s for _, signals in crits for s in signals][:6]

    overlap = []
    for other in concurrent:
        shared = sorted(set(changed) & {
            f["file"] for f in other["forecast"]["files"]})
        if shared:
            overlap.append({
                "with": other["label"], "agent": other["agent"],
                "shared_files": shared,
            })

    terms = contract_terms(repo, changed, repo_path, fork, head)
    scored = score_change(repo, change, failures, crit, overlap)
    hot = sorted(
        ((churn(repo_path, f), f) for f in changed if f in repo["files"]),
        reverse=True,
    )[:3]

    return {
        "label": label or head,
        "agent": agent,
        "task": task,
        "base": base,
        "head": head,
        "changed_files": changed,
        "signature_changes": change["signature_changes"],
        "intent": intent,
        "intent_contradictions": contradictions,
        "blast_radius": {
            "direct": layers[0] if layers else [],
            "indirect": [f for layer in layers[1:] for f in layer],
            "size": "large" if sum(map(len, layers)) > 8 else
                    "medium" if sum(map(len, layers)) > 2 else "small",
        },
        "criticality": crit,
        "criticality_evidence": crit_signals,
        "potential_failures": sorted(
            failures, key=lambda f: -SEVERITY_WEIGHT.get(f["severity"], 0)),
        "concurrent_overlap": overlap,
        "contract_terms": {k: sorted(v) for k, v in terms.items()},
        "history": [{"file": f, "commits": n} for n, f in hot if n],
        **scored,
        "recommendations": recommend(failures, overlap, contradictions),
    }


def recommend(failures, overlap, contradictions):
    out = []
    for failure in failures[:3]:
        if "now requires" in failure["title"] and failure["affected"]:
            out.append(f"Update the {len(failure['affected'])} call site(s) in the "
                       "same change, or give the new parameter a default.")
        elif "no longer exists" in failure["title"]:
            out.append("Leave the old name in place as an alias until the "
                       "importers have moved.")
        elif "Stored data changes" in failure["title"]:
            out.append("Check what is already stored before applying the "
                       "constraint; existing rows will not migrate themselves.")
        elif "Tests cover this" in failure["title"]:
            out.append("Run the tests that import this before merging.")
    for item in overlap:
        out.append(f"Talk to {item['agent']} — they are in "
                   f"{', '.join(item['shared_files'][:2])} as well.")
    if contradictions:
        out.append("Reread the commit message against the diff; they do not "
                   "appear to describe the same change.")
    return out[:6]


def contract_terms(repo, change_files, repo_path, base, head):
    """Field names a change touches that also appear in stored-data files.

    Two changes can meet without sharing a file or an import: one alters the
    column, another stops sending it. The only thing they have in common is
    the name of the field, so that is what this looks for — and only names
    that really appear in a schema file, so it stays evidence rather than
    word association.
    """
    schema_text = ""
    for path in repo.get("schema_files", []):
        try:
            schema_text += (Path(repo["repo"]) / path).read_text(errors="replace")
        except OSError:
            continue
    columns = set(re.findall(r"^\s*(\w+)\s+(?:TEXT|INTEGER|VARCHAR|BOOLEAN|"
                             r"TIMESTAMP|INT|SERIAL|UUID|NUMERIC)",
                             schema_text, re.M | re.I))
    columns |= set(re.findall(r"\b(\w+)\s+(?:IS\s+NOT\s+NULL|SET\s+NOT\s+NULL|"
                              r"IS\s+NULL)", schema_text, re.I))
    if not columns:
        # a repository with no schema to read still has to answer in the shape
        # everyone reads: two named sets, both empty
        return {"mentioned": set(), "stored": set()}
    def terms_in(paths):
        if not paths:
            return set()
        try:
            patch = git(repo_path, "diff", "--unified=0", f"{base}..{head}",
                        "--", *paths)
        except Exception:
            return set()
        changed_lines = "\n".join(
            line for line in patch.splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))
        )
        words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", changed_lines))
        return {c for c in columns if c in words}

    stored = [f for f in change_files if f in repo.get("schema_files", [])]
    return {
        "mentioned": terms_in(list(change_files)),
        # a field whose stored definition changed is the one that matters; the
        # rest are just words that happen to appear in both diffs
        "stored": terms_in(stored),
    }


def interactions(analyses):
    """Changes that are individually calm and jointly dangerous.

    Two people editing the same file is obvious. What this looks for is two
    changes meeting at the same contract from different sides — one changing
    what is stored, another changing who reads it — which neither diff shows
    on its own.
    """
    found = []
    for i, a in enumerate(analyses):
        for b in analyses[i + 1:]:
            shared_files = sorted(set(a["changed_files"]) & set(b["changed_files"]))
            a_syms = {s["symbol"] for s in a["signature_changes"]}
            b_syms = {s["symbol"] for s in b["signature_changes"]}
            shared_syms = sorted(a_syms & b_syms)

            # the interesting case: different files, same blast radius
            a_reach = set(a["blast_radius"]["direct"]) | set(a["changed_files"])
            b_reach = set(b["blast_radius"]["direct"]) | set(b["changed_files"])
            shared_reach = sorted(a_reach & b_reach)

            # both changes mention it, and at least one of them alters how it
            # is stored — otherwise "id" and "name" match everything
            mentioned = (set(a.get("contract_terms", {}).get("mentioned", ()))
                         & set(b.get("contract_terms", {}).get("mentioned", ())))
            anchored = (set(a.get("contract_terms", {}).get("stored", ()))
                        | set(b.get("contract_terms", {}).get("stored", ())))
            shared_contract = sorted(mentioned & anchored)

            if not (shared_files or shared_syms or shared_reach or shared_contract):
                continue

            worst = max(a["risk_score"], b["risk_score"])
            combined = worst
            evidence = []
            if shared_syms:
                combined += 16
                evidence.append(
                    f"both change {', '.join(shared_syms[:3])}")
            if shared_files:
                combined += 10
                evidence.append(f"both edit {', '.join(shared_files[:3])}")
            if shared_reach and not shared_files:
                combined += 12
                evidence.append(
                    "they do not touch the same files, but they meet at "
                    + ", ".join(shared_reach[:3])
                )
            if shared_contract:
                combined += 14
                evidence.append(
                    "both touch "
                    + ", ".join(shared_contract[:3])
                    + ", and one of them changes how it is stored — they are "
                    "approaching the same field from different sides"
                )
            a_kinds = {f["title"].split()[0] for f in a["potential_failures"]}
            b_kinds = {f["title"].split()[0] for f in b["potential_failures"]}
            if "Stored" in a_kinds and "Stored" not in b_kinds and shared_reach:
                combined += 10
                evidence.append(
                    "one side changes what is stored while the other changes "
                    "code that reads it"
                )

            # 100 would claim certainty the evidence does not support
            combined = min(combined, 97)
            found.append({
                "between": [a["label"], b["label"]],
                "agents": [a.get("agent"), b.get("agent")],
                "shared_files": shared_files,
                "shared_symbols": shared_syms,
                "meeting_points": shared_reach[:6],
                "shared_contract": shared_contract,
                "individual": [a["risk_score"], b["risk_score"]],
                "combined_score": combined,
                "combined_band": band(combined),
                # Worse together than apart. Measured against the headroom
                # left, not a fixed gap: once one side is already at 87 there
                # are only ten points to move, and a flat threshold would call
                # that calm. Either the band moves, or the pair closes a real
                # share of the distance to the top of the scale.
                "escalates": band(combined) != band(worst)
                             or combined - worst >= max(6, 0.35 * (97 - worst)),
                "evidence": evidence,
            })
    return sorted(found, key=lambda i: -i["combined_score"])


def repository_risk(analyses, found_interactions):
    """One number for the whole repository, and what is driving it."""
    if not analyses:
        return {"score": 0, "band": "low", "drivers": [],
                "note": "Nothing is in flight."}
    top = max(a["risk_score"] for a in analyses)
    escalating = [i for i in found_interactions if i["escalates"]]
    # 100 out of 100 claims a certainty nothing here has; individual scores
    # are capped the same way
    score = min(97, top + 6 * len(escalating))
    drivers = [
        f"{a['label']} is {a['risk_band']} on its own"
        for a in sorted(analyses, key=lambda a: -a["risk_score"])[:3]
    ]
    drivers += [
        f"{' and '.join(i['between'])} together read as {i['combined_band']}"
        for i in escalating[:2]
    ]
    return {
        "score": score,
        "band": band(score),
        "drivers": drivers,
        "changes": len(analyses),
        "interactions": len(found_interactions),
        "escalating": len(escalating),
    }
