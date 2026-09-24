# what is missing, and what it would take

Written against the PRD. P0 is essentially done; what follows is the honest
list of what is thin, what is absent, and what I would not build.

## thin, not missing

**TypeScript parsing is a regex.** It finds `function foo`, `class Foo` and
`const foo = (` and misses arrow exports, decorators, generics, overloads and
anything clever. Python goes through `ast` and is solid. The PRD asks for
tree-sitter; the fix is to drop tree-sitter in behind `_parse_ts` in
`scan.py`, which is already isolated for exactly this. Half a day, one
dependency, no changes anywhere else. Worth doing before demoing on a
TypeScript repo, not worth doing before that.

**The caller index is a suffix match.** `_callers` decides that
`from api.middleware import x` refers to `api/middleware.py` by comparing
path tails. In a repo with `src/api/middleware.py` and `lib/api/middleware.py`
it will link both. It is a superset, never a subset, so it over-reports rather
than under-reports, which is the right direction for a warning system. Proper
resolution needs per-language module resolution rules and is a day's work per
language.

**The matcher is lexical, plus signals.** Four-letter prefix agreement
between task words and paths or symbols. On words alone it cannot connect
"throttle requests" to `rate_limit`, because they share no letters. Two
things close part of that gap now. `predict` takes optional hints, so a file
that is uncommitted, recently touched, or already claimed by another session
can surface with no vocabulary in common, each saying why it was included.
And `--llm` is a real provider interface (`llm.py`, Anthropic and OpenAI),
opt-in, never on the MCP path, with anything it names that does not exist
dropped. What is still missing is the cheap middle: embed symbol names and
docstrings once per commit, embed the task, take the top-k by cosine, and
keep the lexical matcher as the evidence layer so warnings still explain
themselves. That would beat both on recall without a per-call model bill.

**No concept of a service.** The PRD asks for predicted services and a graph
that spans them. Everything here is file- and symbol-level. For a monorepo
you would infer service boundaries from directory layout or a manifest per
service, then roll file-level risks up. A day, and only worth it against a
repo that actually has services.

## the calibration result, and what it costs us

`prophecy backfill` is built, and it changed what this project can honestly
claim. See the README for how it works and `BACKFILL.md` for the run.

The short version: against real merge history, the risk *level* does not
order text conflicts the way it should. Files marked `high` conflicted less
often than files marked `medium`. That is either a bug in the weighting or a
sign that the loud warnings are catching something text conflicts do not
measure — and the honest position is that we do not yet know which.

The weighting is easy to change and that is exactly the trap. Tuning it
against thirteen conflicting merges would fit noise, and the resulting number
would look like evidence. What it needs instead:

1. Replay several thousand merges across several repos, not sixty in one.
2. Separate the two populations the score currently mixes. `high` comes from
   shared symbols plus a caller bonus, which selects for large, central,
   heavily imported modules. Those may genuinely conflict less in text — a
   big file has more room for two edits to miss each other — while being far
   more dangerous semantically. If so the score is measuring the right thing
   against the wrong outcome.
3. Grade against a better outcome than "did git complain". Merge cleanly,
   then run the test suite. A clean merge with failing tests is the exact
   case this whole project exists for, and `verify --test` already captures
   it; the backfill does not use it yet because running a historical test
   suite needs that commit's dependencies installed.

Until that work happens, the scores are a ranking heuristic that has not
earned the word "calibrated", and the tool should keep saying so.

## absent

**A GitHub Action, and Linear.** `github.py` reads pull requests, forecasts
one and can post the comment; what is missing is the packaged action that
runs `prophecy check` on a pull request and fails it above a threshold.
Linear is an API call that turns issues into the task strings `plan` already
takes. Both are afternoon jobs and neither proves anything the CLI does not
already prove, which is why they are still not done.

**Historical conflict learning.** Mine the repo's own merge history for files
that have conflicted before, and weight risk scores by that. The backfill
already walks exactly that history, so the data is one pass away. Worth doing
after the calibration question above is settled, not before — otherwise it is
one unvalidated heuristic feeding another.

**Backfill with tests.** The replay only asks git whether it could merge. The
more interesting question is whether the merged code still passes, which is
where semantic conflicts actually show up. Needs a per-commit environment,
which is a packaging problem rather than an analysis one.

## what I would not build

The React/Vite/Tailwind/React Flow stack in §7. The dashboard is one HTML
file with no build step, and it shows the dependency graph, branch state,
task forecasting, collision comparison and history. A framework buys
component reuse this does not need yet and costs a toolchain. Revisit if the
dashboard grows past one screen of real interaction.

Multi-repo graphs, runtime behavioural detection, agent orchestration — the
P2 list. Nothing there is reachable from a working P0 in the time available.

## deploying it, and what that settled

**The dashboard is static, the engine is not.** Everything interesting shells
out to git against a real checkout, so no serverless host can run it. That
constraint shaped all three of the ways it ships now.

**The published site is a recording.** `tools/export_static.py` runs the real
server, records every answer and writes them beside the page, so GitHub Pages
serves the whole dashboard reading files instead of an API. Everything that
reads works. Everything that writes says what it needs instead. A workflow
rebuilds it on every change and a check refuses to publish an export that is
broken in the ways nobody would notice.

**Running it is one command.** A multi-architecture image on ghcr, built with
the demo project inside it, started and exercised in CI before it is pushed.
That is what makes the site an on-ramp rather than a brochure: the page hands
somebody their MCP settings and the paste that gives them something to point
at.

**A box is `tools/deploy.sh`.** Clone, venv, systemd, Caddy for TLS. It runs
with `serve --public`, which pins the instance to one repository and refuses
every write, because the code assumes it is talking to the person who started
it and that assumption is false on the internet.

Auth stopped being optional at the same moment, and `serve --auth github` is
the answer: org membership grants access, named people override it, three
roles, and agents carry a token belonging to a person because they cannot
sign in. The one part still untested against reality is the GitHub round
trip itself, which needs a registered OAuth app.

What is still true: a private repository's structure is exactly what this
serves, so an instance reachable by anyone should either require sign-in or
be `--public` and pinned to something you are happy to publish.
