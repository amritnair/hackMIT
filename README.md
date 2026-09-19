# mergemind

Two people pick up two tickets on Monday. Nobody finds out they both rewrite
the same auth helper until Thursday, when one of them rebases.

mergemind tries to move that discovery to Monday. You give it a repo and a
sentence describing work that does not exist yet — no branch, no diff, no
code — and it tells you which files and functions that work will probably
land on, and where two such sentences collide.

```
$ mergemind simulate "Add rate limiting to the API" "Add authentication middleware"

"Add rate limiting to the API"  confidence 0.71
    5.0  api/middleware.py
         symbol rate_limit(request, limit) at api/middleware.py:5 matches 'rate'

"Add authentication middleware"  confidence 0.68
    3.0  api/middleware.py
         path api/middleware.py contains 'middleware'

1 predicted risk(s)

   ! [R1] shared_file (0.53, confidence 0.8)
     Add rate limiting to the API  <->  Add authentication middleware
     - both tasks are forecast to touch api/middleware.py
     - 2 file(s) import api/middleware.py: api/handlers.py, tests/test_middleware.py
     -> Agree on who owns api/middleware.py, or sequence the two tasks.
```

## how it works

`scan` walks the files git knows about and pulls out symbols, signatures and
imports — `ast` for Python, regex for TypeScript and JavaScript. From the
imports it builds a rough reverse index, so it knows that changing
`middleware.py` reaches two other files.

`plan` scores every file in the repo against the words in your task. Path
matches count for more than symbol matches, which count for more than
docstring mentions, and test files get marked down because tests follow the
code rather than lead it. Words that hit nothing get listed separately, under
"no repo evidence for" — if you ask about SMS reminders in a repo that has
never heard of SMS, you should know that before you trust the rest.

`simulate` runs several tasks and compares the results. Same file is worth
flagging. Same file and same function is worth more. Same function in a file
that twelve other files import is worth a conversation before anyone starts.
Schema and migration files and dependency manifests get their own handling,
because two people editing `requirements.txt` in parallel is a different
problem from two people editing a module.

`status` and `predict` do the same job against branches that already exist,
where nothing is a guess: real diffs, real signature changes, real divergence
from the base. A branch that changes `scan(repo)` to `scan(repo, cache)` gets
flagged along with the list of files that import it.

`verify` merges a branch into the base for real, in a throwaway worktree, and
grades the forecast against what happened. Optionally runs your tests in the
merged tree. Your own checkout is never touched.

`context` writes the whole thing out as markdown you can hand to a person or
paste into an agent's prompt. `serve` puts all of it on localhost.

## what it does not do

The matching is lexical. It has no model behind it and no understanding of
what your code means — it knows that a task mentioning "rate limiting" and a
function called `rate_limit` are probably related, and that is most of the
trick. It will miss work that shares no vocabulary with the code it touches,
and it will occasionally point at a file that merely sounds relevant. Every
match prints the evidence that produced it so you can throw out the bad ones
in about a second.

The scores are heuristics, not probabilities. `0.53` means "more than the
other thing on this list", not "53% chance of a conflict". Risk levels are
three buckets because three is about as much precision as the inputs support.

`backfill` is where the claims get tested. It walks the repo's own merge
commits, and for each one rewinds to the point where the two sides diverged,
scans the code as it was *then*, forecasts from the two sets of changes, and
re-runs the merge for real. The forecast never sees the merge commit, so it
cannot cheat.

Two things it deliberately does not do. It does not report recall on text
conflicts as an achievement: a text conflict requires both sides to edit one
file, which is the exact condition that makes this tool fire, so catching
them all is arithmetic rather than skill — missing one would mean a bug. And
it does not report any rate at all until at least ten conflicting merges have
been replayed.

What it does report is the conflict rate broken down by risk level, which is
the only number that tests whether the scoring means anything. If `high` does
not conflict more often than `medium`, the score is decoration, and the tool
says so in those words.

Be careful reading a clean merge as a cleared forecast. Text conflicts were
never the claim — most of what this flags is semantic, and git will happily
merge two compatible-looking edits that break each other at runtime.

## running it

Python 3.10 or newer, no dependencies.

```
pip install -e .

mergemind scan                     # what is in the repo
mergemind status                   # what every branch is doing
mergemind plan "Add SMS reminders" # forecast one task
mergemind predict                  # risks between existing branches
mergemind simulate "task one" "task two" --branch some-branch
mergemind context "Add rate limiting" --against "Refactor request handling"
mergemind explain R2fa01f          # one risk in full, from any earlier run
mergemind verify some-branch --test "pytest"
mergemind insights
mergemind serve                    # dashboard on localhost:8000
```

The dashboard is one HTML file served by the standard library. There is no
build step and no node_modules. State goes in `.mergemind/mergemind.db`.

`-C <path>` points it at another repo. `--json` on any command gives you the
whole structure instead of the summary.

```
python test_mergemind.py
```

builds a small fixture repo in a temp directory and runs the pipeline over it.
