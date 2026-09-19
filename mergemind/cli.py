"""Forecast which parts of a repo a planned task will touch, and where two
pieces of work collide."""

import argparse
import json
import shlex
import sys

from . import store
from . import github, llm
from .agent import brief, observations, request_skeleton
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
    ag.add_argument("--agent", help="who this brief is for; used to keep an "
                                    "agent from being handed its own notes")
    ag.add_argument("--exact", action="store_true",
                    help="count tokens through the API instead of estimating")
    ag.add_argument("--request", action="store_true",
                    help="print a Messages request with the cache breakpoint placed")

    note = sub.add_parser("note",
                          help="record what an agent found, for the next one")
    note.add_argument("file")
    note.add_argument("text")
    note.add_argument("--agent", required=True, help="who found it")

    sub.add_parser("usage", help="how agents used this and what sharing saved")

    fleet = sub.add_parser("fleet",
                           help="find the work in flight and brief everyone on it")
    fleet.add_argument("tasks", nargs="*", help="extra work not yet in a branch")
    fleet.add_argument("--confirm", action="store_true",
                       help="apply the plan; without this it only describes it")

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

    mcp_cmd = sub.add_parser(
        "mcp", help="run as an MCP server so agents can reach this mid-session")
    mcp_cmd.add_argument("--config", action="store_true",
                         help="print the client config instead of running")

    sub.add_parser("sessions", help="which agents are working here right now")

    serve = sub.add_parser("serve", help="dashboard on localhost")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "mcp":
        from .mcp import config_snippet, serve as serve_mcp
        if args.config:
            print(json.dumps(config_snippet(args.repo), indent=2))
            return 0
        return serve_mcp(args.repo)
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
    data = brief(repo, forecasts, found_risks, exact=args.exact,
                 db=db, agent=args.agent, store=store)

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
        print(f"  cached prefix                  {data['prefix_tokens']:>8}"
              f"   #{data['prefix_hash']}")
        for s in data["suffixes"]:
            shared = (f"   +{s['notes_pulled']} note(s) from other agents"
                      if s.get("notes_pulled") else "")
            print(f"  task: {s['task'][:24]:<24} {s['tokens']:>8}{shared}")
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


def _work_in_flight(repo, args, db=None):
    """Find who is already working here, without being told.

    Branches and open pull requests are the work that exists. Anything typed
    in is added on top. Each gets an owner, because the point is to know who
    to talk to, not just which file is busy.
    """
    work = []
    for session in store.live_sessions(db) if db is not None else []:
        if not session["task"]:
            continue
        work.append({
            "agent": session["agent"],
            "label": f"{session['agent']}: {session['task']}",
            "kind": "session",
            "forecast": predict(repo, session["task"]),
        })
    for name, info in branches(repo["repo"], args.base).items():
        if "error" in info or not info["changed_files"]:
            continue
        shaped = as_forecast(info)
        work.append({
            "agent": info.get("author") or name,
            "label": f"branch {name}",
            "kind": "branch",
            "forecast": shaped,
        })
    try:
        for pull in github.pull_requests(repo["repo"]):
            shaped = github.forecast(repo["repo"], pull)
            work.append({
                "agent": shaped["pull_request"]["author"],
                "label": f"PR #{pull['number']} {pull['title']}",
                "kind": "pull_request",
                "forecast": shaped,
            })
    except Exception:
        pass  # no GitHub here; branches alone are plenty
    for task in getattr(args, "tasks", []) or []:
        work.append({
            "agent": task[:24], "label": task, "kind": "task",
            "forecast": predict(repo, task),
        })
    return work


def cmd_fleet(repo, args, db):
    work = _work_in_flight(repo, args, db)
    if not work:
        message = ("Nothing is in flight here — no branches with changes, no "
                   "open pull requests. Name some work and I will plan for it.")
        if not args.json:
            print(message)
        return {"work": [], "summary": [message]}

    forecasts = [w["forecast"] for w in work]
    found = risks(repo, forecasts)

    # Key overlap on the work, not the person. One developer running two
    # agents on the same file is the exact case this is for, and keying on
    # who owns the branch makes that collision disappear.
    touching = {}
    for w in work:
        for f in w["forecast"]["files"]:
            touching.setdefault(f["file"], []).append(w)
    overlap = sorted(
        ({"file": path, "work": [w["label"] for w in items],
          "agents": sorted({w["agent"] for w in items})}
         for path, items in touching.items() if len(items) > 1),
        key=lambda o: (-len(o["work"]), o["file"]),
    )

    # what mergemind can tell the next agent without anyone writing it down
    derived = observations(repo, work)
    fresh = [o for o in derived if not store.note_exists(db, o["file"], o["note"])]

    apply_it = args.confirm
    data = brief(repo, forecasts, found,
                 db=db if apply_it else None,
                 agent=work[0]["agent"] if apply_it else None,
                 store=store if apply_it else None)
    if apply_it:
        for note in fresh:
            store.add_note(db, repo["sha"], note["agent"], note["file"], note["note"])
            store.log(db, repo["sha"], "shared", "mergemind",
                      f"{note['file']}: {note['note'][:70]}")
        store.save_risks(db, found)
        for w, suffix in zip(work[1:], data["suffixes"][1:]):
            store.record_brief(
                db, repo["sha"], w["agent"], w["forecast"]["task"],
                data["prefix_hash"], data["prefix_tokens"], suffix["tokens"],
                data["economics"]["repo_if_each_agent_reads_every_file"]
                // max(1, len(work)),
            )

    e = data["economics"]
    people = sorted({w["agent"] for w in work})
    shared_risks = [r for r in found if len(r["tasks"]) == 2]
    high_shared = [r for r in shared_risks if r["risk_level"] == "high"]
    solo_high = [r for r in found
                 if len(r["tasks"]) == 1 and r["risk_level"] == "high"]
    summary = [
        f"{len(work)} piece(s) of work in flight here, from "
        f"{len(people)} person or agent: {', '.join(people[:5])}.",
        (f"{len(overlap)} file(s) are being changed by more than one of them: "
         + ", ".join(o["file"] for o in overlap[:3]) + ".")
        if overlap else "Nobody is changing the same file as anyone else.",
    ]
    if high_shared:
        summary.append(
            f"{len(high_shared)} of those overlaps are worth sorting out "
            "before the work lands, rather than at merge time."
        )
    if solo_high:
        summary.append(
            f"Separately, {len(solo_high)} change(s) alter an interface other "
            "files depend on, which affects whoever imports them."
        )
    if fresh:
        summary.append(
            f"{len(fresh)} thing(s) worth telling whoever works here next — "
            "who else is in each file, which signatures are about to change, "
            "which files to regenerate rather than merge. Read from the "
            "repository, so nobody has to write them down."
        )
    summary.append(
        f"Everyone here needs the same {data['prefix_tokens']:,} tokens of "
        "background about this repository. Sent once and cached, each agent "
        f"then costs about {e['brief_per_later_call']:,} tokens a turn instead "
        f"of the {e['repo_if_each_agent_reads_every_file']:,} it takes to read "
        "the repo from scratch."
    )
    if not data["cacheable"]:
        summary.append(
            "This repository is small enough that the shared half lands under "
            "the size a model will cache, so treat the saving as a briefing "
            "convenience rather than a billing one."
        )

    out = {
        "work": [{k: v for k, v in w.items() if k != "forecast"} for w in work],
        "assignments": [
            {"agent": w["agent"], "label": w["label"], "kind": w["kind"],
             "files": [f["file"] for f in w["forecast"]["files"]],
             "tokens": suffix["tokens"],
             "notes_pulled": suffix.get("notes_pulled", 0)}
            for w, suffix in zip(work, data["suffixes"])
        ],
        "overlap": overlap,
        "risks": found,
        "economics": e,
        "prefix_tokens": data["prefix_tokens"],
        "cacheable": data["cacheable"],
        "observations": fresh,
        "recorded": apply_it,
        "summary": summary,
    }
    if not args.json:
        for line in summary:
            print(line)
        print()
        for row in out["assignments"]:
            print(f"  {row['agent'][:22]:<22} {row['label'][:40]:<40} "
                  f"{len(row['files'])} file(s)")
        if overlap:
            print("\n  shared ground:")
            for item in overlap:
                print(f"    {item['file']:<44} {', '.join(item['agents'])}")
        if fresh:
            print("\n  would tell the next agent:")
            for note in fresh[:6]:
                print(f"    {note['file']:<32} {note['note'][:70]}")
        print("\n" + ("Applied: briefs recorded and findings shared."
                       if apply_it else
                       "Nothing recorded yet. Re-run with --confirm to apply."))
    return out


def cmd_sessions(repo, args, db):
    live = store.live_sessions(db)
    events = store.feed(db, 15)
    if not args.json:
        if not live:
            print("No agent sessions are live here. Start one with "
                  "`mergemind mcp` wired into an agent, or see `mcp --config`.")
        for row in live:
            print(f"  {row['agent'][:20]:<20} {row['task'][:46]:<46} "
                  f"since {row['joined_at']}")
        if events:
            print("\n  recent")
            for e in reversed(events):
                print(f"    {e['at']}  {e['kind']:<8} {e['agent'][:16]:<16} "
                      f"{e['detail'][:60]}")
    return {"live": live, "events": events}


def cmd_note(repo, args, db):
    if args.file not in repo["files"]:
        print(f"{args.file} is not a code file in this repo", file=sys.stderr)
        return {"error": "unknown file"}
    store.add_note(db, repo["sha"], args.agent, args.file, args.text)
    if not args.json:
        print(f"recorded against {args.file}. The next agent sent to that file "
              "gets it in their brief.")
    return {"file": args.file, "agent": args.agent, "note": args.text}


def cmd_usage(repo, args, db):
    data = store.usage(db)
    if not args.json:
        if not data["briefs"]:
            print(data["note"])
            return data
        print(f"{data['briefs']} brief(s) issued to {data['agents']} agent(s)")
        print(f"  prefix sent in full        {data['prefix_first_time']:>8}")
        print(f"  prefix already seen        {data['prefix_reused']:>8}"
              f"   ({data['reuse_rate'] * 100:.0f}% reuse)")
        print(f"  tokens actually sent       {data['tokens_sent']:>8}")
        print(f"  if each read the repo      "
              f"{data['tokens_if_each_agent_read_the_repo']:>8}")
        print(f"  avoided                    {data['tokens_avoided']:>8}")
        print(f"\n  notes written {data['notes_written']}, "
              f"pulled into briefs {data['notes_pulled']}")
        if data["per_agent"]:
            print("\n  per agent")
            for row in data["per_agent"]:
                print(f"    {row['agent'][:28]:<28} {row['briefs']:>3} brief(s)"
                      f"  {row['task_tokens'] or 0:>7} task tokens"
                      f"  {row['notes_pulled'] or 0:>3} note(s) received")
        if data["shared_files"]:
            print("\n  files more than one agent has been into")
            for row in data["shared_files"]:
                print(f"    {row['file']:<40} {row['agents']} agent(s), "
                      f"{row['n']} note(s)")
        print(f"\n{data['note']}")
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
    "note": cmd_note, "usage": cmd_usage, "fleet": cmd_fleet,
    "sessions": cmd_sessions,
}

if __name__ == "__main__":
    sys.exit(main())
