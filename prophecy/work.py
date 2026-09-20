"""Everything being worked on right now, from whatever source knows about it.

Three places hold that answer and none of them holds all of it: live agent
sessions know what is happening this minute, branches know what is half
finished, and GitHub knows what is waiting on review. An agent that only sees
one of the three gets told about a teammate's session but not their pull
request, which is exactly the collision people actually hit.
"""

from .branches import as_forecast, branches
from .predict import predict


def in_flight(repo, base="main", db=None, store=None, tasks=()):
    work = []

    if db is not None and store is not None:
        for session in store.live_sessions(db):
            if session["task"]:
                work.append({
                    "agent": session["agent"],
                    "label": f"{session['agent']}: {session['task']}",
                    "kind": "session",
                    "forecast": dict(predict(repo, session["task"]),
                                     owner=session["agent"]),
                })

    for name, info in branches(repo["repo"], base).items():
        if "error" in info or not info["changed_files"]:
            continue
        work.append({
            "agent": info.get("author") or name,
            "label": f"branch {name}",
            "kind": "branch",
            "forecast": dict(as_forecast(info),
                             owner=info.get("author") or name),
        })

    try:
        from . import github
        for pull in github.pull_requests(repo["repo"]):
            shaped = github.forecast(repo["repo"], pull)
            shaped["owner"] = shaped["pull_request"]["author"]
            work.append({
                "agent": shaped["pull_request"]["author"],
                "label": f"PR #{pull['number']} {pull['title']}",
                "kind": "pull_request",
                "forecast": shaped,
            })
    except Exception:
        pass  # no GitHub remote, or gh is not logged in; the rest still works

    for task in tasks:
        work.append({
            "agent": task[:24], "label": task, "kind": "task",
            "forecast": predict(repo, task),
        })
    return work
