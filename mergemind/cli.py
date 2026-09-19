import argparse
import json
import sys

from .predict import predict
from .risk import capsule, risks, strategies
from .scan import scan

BAR = {"high": "!!", "medium": " !", "low": "  "}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mergemind", description=__doc__)
    parser.add_argument("-C", "--repo", default=".", help="repository to analyse")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="summarise what is in the repo")

    plan = sub.add_parser("plan", help="forecast the impact of one task")
    plan.add_argument("task")

    sim = sub.add_parser("simulate", help="check several tasks against each other")
    sim.add_argument("tasks", nargs="+")

    ctx = sub.add_parser("context", help="markdown briefing for a task")
    ctx.add_argument("task")
    ctx.add_argument("--against", action="append", default=[],
                     help="other in-flight task (repeatable)")

    args = parser.parse_args(argv)
    repo = scan(args.repo)
    out = COMMANDS[args.cmd](repo, args)
    if args.json:
        json.dump(out, sys.stdout, indent=2)
        print()
    return 0


def cmd_scan(repo, args):
    files = repo["files"]
    symbols = sum(len(f["symbols"]) for f in files.values())
    if not args.json:
        print(f"{repo['repo']} @ {repo['sha'][:10]} ({repo['branch']})")
        print(f"  {len(files)} code files, {symbols} symbols")
        print(f"  {sum(1 for f in files.values() if f['is_test'])} test files, "
              f"{len(repo['schema_files'])} schema/migration files")
        hot = sorted(repo["callers"].items(), key=lambda kv: -len(kv[1]))[:5]
        if hot:
            print("  most depended on:")
            for rel, callers in hot:
                print(f"    {rel} <- {len(callers)} file(s)")
    return {k: v for k, v in repo.items() if k != "files"} | {"file_count": len(files)}


def cmd_plan(repo, args):
    forecast = predict(repo, args.task)
    if not args.json:
        _print_forecast(forecast)
    return forecast


def cmd_simulate(repo, args):
    forecasts = [predict(repo, t) for t in args.tasks]
    found = risks(repo, forecasts)
    plans = strategies(repo, forecasts, found)
    if not args.json:
        for forecast in forecasts:
            _print_forecast(forecast)
            print()
        print(f"{len(found)} predicted risk(s)")
        for r in found:
            print(f"\n  {BAR[r['risk_level']]} [{r['id']}] {r['risk_type']} "
                  f"({r['risk_score']}, confidence {r['confidence']})")
            print(f"     {r['tasks'][0]}  <->  {r['tasks'][1]}")
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


def cmd_context(repo, args):
    forecast = predict(repo, args.task)
    others = [predict(repo, t) for t in args.against]
    found = risks(repo, [forecast, *others])
    markdown = capsule(repo, forecast, found)
    if not args.json:
        print(markdown)
    return {"forecast": forecast, "risks": found, "markdown": markdown}


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
    "scan": cmd_scan, "plan": cmd_plan,
    "simulate": cmd_simulate, "context": cmd_context,
}

if __name__ == "__main__":
    sys.exit(main())
