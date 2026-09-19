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

**The matcher is lexical.** Four-letter prefix agreement between task words
and paths/symbols. It cannot connect "throttle requests" to `rate_limit`
because they share no letters. This is the real ceiling on prediction
quality, and it is the one gap where a model genuinely helps: embed symbol
names and docstrings once per commit, embed the task, take the top-k by
cosine, and keep the lexical matcher as the evidence layer so warnings still
explain themselves. That is the `LLMProvider` seam in §7 and it does not
exist yet — there is no provider interface in the codebase at all, because
one implementation behind an interface is just a file with extra steps. Add
the interface when the second implementation shows up.

**No concept of a service.** The PRD asks for predicted services and a graph
that spans them. Everything here is file- and symbol-level. For a monorepo
you would infer service boundaries from directory layout or a manifest per
service, then roll file-level risks up. A day, and only worth it against a
repo that actually has services.

## absent

**Forecast calibration.** `verify` records every merge outcome into SQLite,
and `insights` refuses to report accuracy under ten runs. Getting past that
means running `verify` over historical merges — replay the last few hundred
merge commits in a real repo, forecast each from its fork point, compare
against what happened. That backfill script is the single most valuable thing
left to build, and it is maybe 80 lines on top of what is already here, since
`trial_merge` and `compare` do the work. It converts "we think this helps"
into a number. Everything else on this list is polish next to it.

**GitHub and Linear.** `verify` already does the hard part; a GitHub Action
is `mergemind predict --json` plus a comment-posting step, and PR analysis is
branch analysis with a different name. Linear is an API call that turns
issues into the task strings `plan` already takes. Both are afternoon jobs
and neither proves anything the CLI does not already prove, which is why they
are not done.

**Historical conflict learning.** Mine the repo's own merge history for files
that have conflicted before, and weight risk scores by that. Genuinely
useful, genuinely cheap, needs the backfill above to be worth trusting.

## what I would not build

The React/Vite/Tailwind/React Flow stack in §7. The dashboard is one HTML
file with no build step, and it shows the dependency graph, branch state,
task forecasting, collision comparison and history. A framework buys
component reuse this does not need yet and costs a toolchain. Revisit if the
dashboard grows past one screen of real interaction.

Multi-repo graphs, runtime behavioural detection, agent orchestration — the
P2 list. Nothing there is reachable from a working P0 in the time available.

## deploying it

Worth knowing before anyone tries: **the dashboard is static, the engine is
not.** Everything interesting shells out to git against a real checkout, so
it cannot run on a serverless host with no repo on disk. Three options:

1. **Local, as designed.** `mergemind serve`. This is the honest demo.
2. **Static dashboard on Vercel, data baked in.** Run the commands, dump the
   JSON, ship dashboard + JSON as a static site. Deploys free, demos fine,
   but it is a recording, not a tool.
3. **Dashboard on Vercel, engine on a box with the repo cloned.** A real
   split, and the point at which auth stops being optional, since the API
   would serve up a private repo's structure to anyone who asks.

Vercel's free Hobby plan runs unlimited projects, so two sites under one
account is fine — the limits that bite are account-wide bandwidth and the
non-commercial-use terms, neither of which a hackathon touches.
