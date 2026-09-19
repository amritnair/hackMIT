"""Forecast which parts of a repo a planned task will touch, and where two
pieces of work collide."""

import argparse
import json
import shlex
import sys

from . import store
from . import github, llm
from .agent import brief, request_skeleton
from .backfill import replay, report
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
    parser.add_argument("--llm", action="store_true",
                        help="also ask a model where the work lands; "
                             "costs tokens, off by default")
    parser.add_argument("--provider", choices=sorted(llm.PROVIDERS),
                        help="which model provider --llm should use")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="summarise what is in the repo")
    sub.add_parser("status", help="what every branch is doing right now")
    sub.add_parser("insights", help="what this repo has taught us so far")

    plan = sub.add_parser("plan", help="forecast the impact of one task")
    plan.add_argument("task")

    sub.add_parser("predict", help="risks between the branches that already exist")

    pr = sub.add_parser("pr", help="check open pull requests against each other")
    pr.add_argument("numbers", nargs="*", type=int,
                    help="specific PRs; default is every open one")
    pr.add_argument("--comment", type=int, metavar="N",
                    help="post the findings as a comment on PR N")

    sim = sub.add_parser("simulate", help="check several tasks against each other")
    sim.add_argument("tasks", nargs="*")
    sim.add_argument("--branch", action="append", default=[],
                     help="include a real branch in the comparison (repeatable)")

    ctx = sub.add_parser("context", help="markdown briefing for a task")
    ctx.add_argument("task")
    ctx.add_argument("--against", action="append", default=[],
                     help="other in-flight task (repeatable)")

    ag = sub.add_parser("brief",
                        help="cache-shaped context for one or more agents")
    ag.add_argument("tasks", nargs="+")
    ag.add_argument("--branch", action="append", default=[],
                    help="include a real branch as another agent's work")
    ag.add_argument("--exact", action="store_true",
                    help="count tokens through the API instead of estimating")
    ag.add_argument("--request", action="store_true",
                    help="print a Messages request with the cache breakpoint placed")

    explain = sub.add_parser("explain", help="show one risk in full")
    explain.add_argument("risk_id")

    verify = sub.add_parser("verify", help="really merge a branch and grade the forecast")
    verify.add_argument("branch")
    verify.add_argument("--test", help="command to run after a clean merge")

    back = sub.add_parser("backfill",
                          help="replay old merges and grade the forecast")
    back.add_argument("--limit", type=int, default=50,
                      help="how many merge commits to replay")
    back.add_argument("--ref", default="HEAD", help="history to walk")

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


def _forecast(repo, task, args):
    """Lexical always; the model only when asked, and never silently."""
    lexical = predict(repo, task)
    if not getattr(args, "llm", False):
        return lexical
    try:
        provider = llm.get_provider(args.provider)
        semantic = llm.semantic_predict(repo, task, provider)
    except llm.NoProvider as exc:
        print(f"no model used: {exc}", file=sys.stderr)
        return lexical
    except Exception as exc:
        print(f"model call failed, falling back to repository evidence: {exc}",
              file=sys.stderr)
        return lexical
    return llm.merge_forecasts(lexical, semantic)


def cmd_plan(repo, args, db):
    forecast = _forecast(repo, args.task, args)
    if not args.json:
        _print_forecast(forecast)
    return forecast


def cmd_pr(repo, args, db):
    try:
        open_prs = github.pull_requests(repo["repo"])
    except github.GitHubUnavailable as exc:
        print(f"github unavailable: {exc}", file=sys.stderr)
        return {"error": str(exc)}

    wanted = [p for p in open_prs if not args.numbers or p["number"] in args.numbers]
    if not wanted:
        if not args.json:
            print("no open pull requests" if not open_prs
                  else f"none of {args.numbers} are open")
        return {"pull_requests": open_prs, "risks": []}

    forecasts = []
    for pull in wanted:
        try:
            forecasts.append(github.forecast(repo["repo"], pull))
        except Exception as exc:
            print(f"could not read PR #{pull['number']}: {exc}", file=sys.stderr)
    found = risks(repo, forecasts)
    store.save_risks(db, found)

    if args.comment:
        body = github.comment_body(args.comment, found, repo["sha"])
        url = github.post_comment(repo["repo"], args.comment, body)
        if not args.json:
            print(f"commented on #{args.comment}: {url}")
        return {"commented": args.comment, "body": body, "risks": found}

    if not args.json:
        print(f"{len(wanted)} open pull request(s)")
        for pull in wanted:
            print(f"  #{pull['number']} {pull['title']}"
                  f"  ({(pull.get('author') or {}).get('login', '?')}"
                  f"{', draft' if pull.get('isDraft') else ''})")
        print(f"\n{len(found)} predicted risk(s) between them")
        for r in found:
            print(f"\n  {MARK[r['risk_level']]} [{r['id']}] {r['risk_type']}")
            print(f"     {'  <->  '.join(r['tasks'])}")
            for line in r["evidence"]:
                print(f"     - {line}")
            print(f"     -> {r['recommendation']}")
        if found:
            print("\nto leave this on a pull request: "
                  f"mergemind pr --comment {wanted[0]['number']}")
    return {"pull_requests": wanted, "risks": found}


def cmd_predict(repo, args, db):
    found = branches(repo["repo"], args.base)
    forecasts = [as_forecast(b) for b in found.values() if "error" not in b]
    return _collide(repo, args, db, forecasts, header="branches in flight")


def cmd_simulate(repo, args, db):
    found = branches(repo["repo"], args.base)
    forecasts = [_forecast(repo, t, args) for t in args.tasks]
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


def cmd_brief(repo, args, db):
    found = branches(repo["repo"], args.base)
    forecasts = [predict(repo, t) for t in args.tasks]
    forecasts += [as_forecast(found[n]) for n in args.branch if n in found]
    found_risks = risks(repo, forecasts)
    store.save_risks(db, found_risks)
    data = brief(repo, forecasts, found_risks, exact=args.exact)

    if args.request:
        if not args.json:
            print(json.dumps(request_skeleton(data), indent=2))
        return request_skeleton(data)

    if not args.json:
        e = data["economics"]
        print("=" * 62)
        print("CACHED PREFIX — same bytes for every agent on this commit")
        print("=" * 62)
        print(data["prefix"])
        for s in data["suffixes"]:
            print()
            print("=" * 62)
            print(f"AFTER THE BREAKPOINT — {s['task']}")
            print("=" * 62)
            print(s["text"])
        print()
        print("-" * 62)
        label = "counted" if data["exact"] else "estimated"
        print(f"tokens ({label})")
        print(f"  cached prefix                  {data['prefix_tokens']:>8}")
        for s in data["suffixes"]:
            print(f"  task: {s['task'][:24]:<24} {s['tokens']:>8}")
        print(f"  {e['agents']} agent(s) reading every code file "
              f"{e['repo_if_each_agent_reads_every_file']:>8}")
        print(f"  this brief, first call         {e['brief_first_call']:>8}")
        print(f"  this brief, once cache is warm {e['brief_per_later_call']:>8}")
        print(f"  difference per later call      {e['saved_per_later_call']:>8}"
              f"   ~${e['dollars_per_later_call']} vs "
              f"${e['dollars_if_each_agent_reads_every_file']}")
        print(f"\n{e['note']}")
        for warning in data["warnings"]:
            print(f"\nwarning: {warning}")
    return data


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
    forecasts = [as_forecast(target), *others]
    predicted = risks(repo, forecasts)
    store.save_risks(db, predicted)

    outcome = trial_merge(repo["repo"], args.base, args.branch,
                          shlex.split(args.test) if args.test else None)
    result = compare(predicted, outcome, forecasts)
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


def cmd_backfill(repo, args, db):
    if not args.json:
        print(f"replaying up to {args.limit} merge(s) from {args.ref}")

    def progress(run):
        if args.json:
            return
        mark = "conflict" if run["conflicted"] else "clean   "
        detail = ""
        if run["conflicted"]:
            detail = (f"  caught {len(run['caught'])}/{len(run['conflicted'])}"
                      f"{'  MISSED ' + ', '.join(run['missed']) if run['missed'] else ''}")
        print(f"  {run['merge'][:8]}  {mark}  {run['risks']} risk(s){detail}")

    runs = replay(repo["repo"], args.limit, args.ref, progress)
    summary = report(runs)
    for run in runs:
        store.save_outcome(
            db, repo,
            {"base": run["merge"][:8] + "^1", "branch": run["merge"][:8] + "^2",
             "merged_clean": not run["conflicted"]},
            {"tests_passed": None, "predicted_and_conflicted": run["caught"],
             "conflicted_unpredicted": run["missed"],
             "predicted_no_conflict": run["flagged_no_conflict"], "notes": []},
            source="backfill",
        )
    if not args.json:
        print(f"\n{summary['merges_replayed']} merge(s) replayed, "
              f"{summary['merges_with_conflicts']} of them conflicted")
        print(f"  files touched across both sides:  {summary['files_in_play']}")
        print(f"  files flagged:                    {summary['files_flagged']}")
        print(f"  files that really conflicted:     {summary['conflicted_files']}"
              f" ({summary['files_caught']} flagged, {summary['files_missed']} missed)")
        if summary["median_lead_hours"] is not None:
            print(f"  median warning lead time:         "
                  f"{summary['median_lead_hours']} hours before the merge")
        if summary["by_level"]:
            print("\n  does the score mean anything?")
            for level, bucket in summary["by_level"].items():
                print(f"    {level:<7} {bucket['flagged']:>5} flagged  "
                      f"{bucket['conflicted']:>4} conflicted  "
                      f"{bucket['rate'] * 100:>5.1f}%")
        print(f"\n{summary['recall_note']}")
        print(f"\n{summary['verdict']}")
    return {"runs": runs, "summary": summary}


def cmd_insights(repo, args, db):
    data = store.insights(db)
    if not args.json:
        print(f"{data['snapshots']} snapshot(s), "
              f"{data['risks_recorded']} risk(s) recorded")
        for kind, n in data["by_type"].items():
            print(f"  {n:>3}  {kind}")
        print(f"\n{data['merges_run']} merge(s) run by hand, "
              f"{data['merged_clean']} clean")
        print(f"{data['merges_replayed']} merge(s) replayed from history")
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
    if forecast.get("grounding"):
        print(f"  {forecast['grounding']}")
    if forecast.get("model_invented"):
        print(f"  model named files that do not exist, dropped: "
              f"{', '.join(forecast['model_invented'])}")


COMMANDS = {
    "scan": cmd_scan, "status": cmd_status, "plan": cmd_plan,
    "predict": cmd_predict, "simulate": cmd_simulate, "context": cmd_context,
    "explain": cmd_explain, "verify": cmd_verify, "insights": cmd_insights,
    "backfill": cmd_backfill, "brief": cmd_brief, "pr": cmd_pr,
}

if __name__ == "__main__":
    sys.exit(main())
