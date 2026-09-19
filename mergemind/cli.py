"""Forecast which parts of a repo a planned task will touch, and where two
pieces of work collide."""

import argparse
import json
import shlex
import sys

from . import store
from .branches import as_forecast, branches
from .merge import compare, trial_merge
from .predict import predict
from .risk import capsule, risks, strategies
from .scan import scan

MARK = {"high": "!!", "medium": " !", "low": "  "}


def build_parser():
    parser = argparse.ArgumentParser(prog="mergemind", description=__doc__)
    parser.add_argument("-C", "--repo", default=".", help="repository to analyse")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--base", default="main", help="branch to compare against")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="summarise what is in the repo")
    sub.add_parser("status", help="what every branch is doing right now")
    sub.add_parser("insights", help="what this repo has taught us so far")

    plan = sub.add_parser("plan", help="forecast the impact of one task")
    plan.add_argument("task")

    sub.add_parser("predict", help="risks between the branches that already exist")

    sim = sub.add_parser("simulate", help="check several tasks against each other")
    sim.add_argument("tasks", nargs="*")
    sim.add_argument("--branch", action="append", default=[],
                     help="include a real branch in the comparison (repeatable)")

    ctx = sub.add_parser("context", help="markdown briefing for a task")
    ctx.add_argument("task")
    ctx.add_argument("--against", action="append", default=[],
                     help="other in-flight task (repeatable)")

    explain = sub.add_parser("explain", help="show one risk in full")
    explain.add_argument("risk_id")

    verify = sub.add_parser("verify", help="really merge a branch and grade the forecast")
    verify.add_argument("branch")
    verify.add_argument("--test", help="command to run after a clean merge")

    serve = sub.add_parser("serve", help="dashboard on localhost")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "serve":
        from .server import serve
        return serve(args.repo, args.port)

    repo = scan(args.repo)
    db = store.connect(args.repo)
    store.save_snapshot(db, repo)
    out = COMMANDS[args.cmd](repo, args, db)
    if args.json:
        json.dump(out, sys.stdout, indent=2, default=str)
        print()
    return 0


def cmd_scan(repo, args, db):
    files = repo["files"]
    if not args.json:
        print(f"{repo['repo']} @ {repo['sha'][:10]} ({repo['branch']})")
        print(f"  {len(files)} code files, "
              f"{sum(len(f['symbols']) for f in files.values())} symbols")
        print(f"  {sum(1 for f in files.values() if f['is_test'])} test files, "
              f"{len(repo['schema_files'])} schema/migration files")
        hot = sorted(repo["callers"].items(), key=lambda kv: -len(kv[1]))[:5]
        if hot:
            print("  most depended on:")
            for rel, callers in hot:
                print(f"    {rel} <- {len(callers)} file(s)")
    return {k: v for k, v in repo.items() if k != "files"} | {"file_count": len(files)}


def cmd_status(repo, args, db):
    found = branches(repo["repo"], args.base)
    live = {n: b for n, b in found.items() if "error" not in b}
    solo = risks(repo, [as_forecast(b) for b in live.values()])
    store.save_risks(db, solo)
    if not args.json:
        print(f"base {args.base} — {len(live)} other branch(es)")
        for name, b in live.items():
            print(f"\n  {name}  +{b['ahead']}/-{b['behind']}  "
                  f"{b['author']}, {b['last_commit']}")
            print(f"    {b['subject']}")
            print(f"    {len(b['changed_files'])} file(s) changed")
            for change in b["signature_changes"][:3]:
                print(f"    signature: {change['before']} -> {change['after']}")
        if solo:
            print(f"\n{len(solo)} risk(s) from branches on their own:")
            for r in solo:
                print(f"  {MARK[r['risk_level']]} [{r['id']}] {r['risk_type']} — "
                      f"{r['evidence'][0]}")
    return {"base": args.base, "branches": found, "risks": solo}


def cmd_plan(repo, args, db):
    forecast = predict(repo, args.task)
    if not args.json:
        _print_forecast(forecast)
    return forecast


def cmd_predict(repo, args, db):
    found = branches(repo["repo"], args.base)
    forecasts = [as_forecast(b) for b in found.values() if "error" not in b]
    return _collide(repo, args, db, forecasts, header="branches in flight")


def cmd_simulate(repo, args, db):
    found = branches(repo["repo"], args.base)
    forecasts = [predict(repo, t) for t in args.tasks]
    forecasts += [as_forecast(found[n]) for n in args.branch if n in found]
    if not forecasts:
        print("give me some tasks, or --branch <name>", file=sys.stderr)
        return {}
    return _collide(repo, args, db, forecasts)


def _collide(repo, args, db, forecasts, header=None):
    found = risks(repo, forecasts)
    plans = strategies(repo, forecasts, found)
    store.save_risks(db, found)
    if not args.json:
        if header:
            print(f"{header}: {', '.join(f['task'] for f in forecasts) or 'none'}\n")
        else:
            for forecast in forecasts:
                _print_forecast(forecast)
                print()
        print(f"{len(found)} predicted risk(s)")
        for r in found:
            print(f"\n  {MARK[r['risk_level']]} [{r['id']}] {r['risk_type']} "
                  f"({r['risk_score']}, confidence {r['confidence']})")
            print(f"     {'  <->  '.join(r['tasks'])}")
            for line in r["evidence"]:
                print(f"     - {line}")
            print(f"     -> {r['recommendation']}")
        print("\nexecution strategies")
        for plan in plans:
            print(f"\n  {plan['strategy']}: {plan['open_risks']} risk(s) left open")
            for step in plan["coordination_steps"]:
                print(f"     - {step}")
            print(f"     {plan['note']}")
    return {"forecasts": forecasts, "risks": found, "strategies": plans}


def cmd_context(repo, args, db):
    forecast = predict(repo, args.task)
    others = [predict(repo, t) for t in args.against]
    found = risks(repo, [forecast, *others])
    store.save_risks(db, found)
    markdown = capsule(repo, forecast, found)
    if not args.json:
        print(markdown)
    return {"forecast": forecast, "risks": found, "markdown": markdown}


def cmd_explain(repo, args, db):
    risk = store.get_risk(db, args.risk_id)
    if not risk:
        print(f"no risk {args.risk_id}. Run scan, simulate or status first.",
              file=sys.stderr)
        return {}
    if not args.json:
        print(f"[{risk['id']}] {risk['risk_type']}  "
              f"{risk['risk_level']} ({risk['risk_score']})")
        print(f"  between: {'  <->  '.join(risk['tasks'])}")
        print(f"  repo at: {risk['sha'][:10]}")
        print(f"  first seen {risk['first_seen']}, last seen {risk['last_seen']}")
        print("  evidence:")
        for line in risk["evidence"]:
            print(f"    - {line}")
        print(f"  recommendation: {risk['recommendation']}")
        print(f"  confidence {risk['confidence']} — a heuristic, not a probability.")
    return risk


def cmd_verify(repo, args, db):
    found = branches(repo["repo"], args.base)
    others = [as_forecast(b) for n, b in found.items()
              if "error" not in b and n != args.branch]
    target = found.get(args.branch)
    if not target or "error" in target:
        print(f"no branch {args.branch}", file=sys.stderr)
        return {}
    predicted = risks(repo, [as_forecast(target), *others])
    store.save_risks(db, predicted)

    outcome = trial_merge(repo["repo"], args.base, args.branch,
                          shlex.split(args.test) if args.test else None)
    result = compare(predicted, outcome)
    store.save_outcome(db, repo, outcome, result)

    if not args.json:
        print(f"merging {args.branch} into {args.base} in a scratch worktree")
        if outcome["error"]:
            print(f"  failed: {outcome['error']}")
        print(f"  clean merge: {outcome['merged_clean']}")
        if outcome["conflicted_files"]:
            print("  conflicts:")
            for rel in outcome["conflicted_files"]:
                print(f"    {rel}")
        if outcome["tests"]:
            print(f"  tests ({outcome['tests']['command']}): "
                  f"{'passed' if outcome['tests']['passed'] else 'FAILED'}")
        print(f"\nforecast said {len(predicted)} risk(s) over "
              f"{len(result['predicted_files'])} file(s)")
        print(f"  predicted and conflicted: {result['predicted_and_conflicted'] or '-'}")
        print(f"  conflicted, unpredicted:  {result['conflicted_unpredicted'] or '-'}")
        print(f"  predicted, no conflict:   {result['predicted_no_conflict'] or '-'}")
        for note in result["notes"]:
            print(f"  note: {note}")
    return {"predicted": predicted, "outcome": outcome, "comparison": result}


def cmd_insights(repo, args, db):
    data = store.insights(db)
    if not args.json:
        print(f"{data['snapshots']} snapshot(s), "
              f"{data['risks_recorded']} risk(s) recorded")
        for kind, n in data["by_type"].items():
            print(f"  {n:>3}  {kind}")
        print(f"\n{data['merges_run']} merge run(s), {data['merged_clean']} clean")
        print(f"forecast accuracy: {data['accuracy']}")
        if data["recurring"]:
            print("\nrisks that keep coming back:")
            for row in data["recurring"]:
                print(f"  [{row['id']}] {row['risk_type']} — {row['tasks']}")
    return data


def _print_forecast(forecast):
    print(f'"{forecast["task"]}"  confidence {forecast["confidence"]}')
    if not forecast["files"]:
        print("  nothing in this repo matched.")
    for f in forecast["files"]:
        print(f"  {f['score']:>5}  {f['file']}")
        for line in f["evidence"][:2]:
            print(f"         {line}")
    if forecast["tests"]:
        print(f"  tests: {', '.join(forecast['tests'])}")
    if forecast["unsupported_terms"]:
        print(f"  no repo evidence for: {', '.join(forecast['unsupported_terms'])}")


COMMANDS = {
    "scan": cmd_scan, "status": cmd_status, "plan": cmd_plan,
    "predict": cmd_predict, "simulate": cmd_simulate, "context": cmd_context,
    "explain": cmd_explain, "verify": cmd_verify, "insights": cmd_insights,
}

if __name__ == "__main__":
    sys.exit(main())
