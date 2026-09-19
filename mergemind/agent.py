"""Build the context an agent actually needs, and nothing else.

An agent dropped into an unfamiliar repo spends its first few thousand tokens
working out where things are. Several agents on the same repo each pay that
bill separately, and pay it again on every turn, because the exploration is
interleaved with the work and cannot be cached.

This module splits the context in two:

  stable   — what is true about the repo regardless of the task. Identical for
             every agent working on this commit, so it goes in the cached
             prefix and is paid for roughly once.
  volatile — what is true about one task. Different per agent, so it goes
             after the last cache breakpoint where it invalidates nothing.

That split is the whole point. Prompt caching is a prefix match rendered in
the order tools → system → messages: one changed byte anywhere in the prefix
invalidates everything after it. Repo facts sorted deterministically at the
front, task text at the back.
"""

from pathlib import Path

# Anthropic list price, input, dollars per million tokens. Used only to turn
# token counts into a number people can reason about; override for other
# models. Cache reads bill at about a tenth of this, writes at about 1.25x.
PRICE_PER_MTOK = 5.00
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

# A prefix shorter than the model's minimum is not cached, and nothing says
# so — the request simply succeeds at full price. The floor is model
# dependent (512 to 4096); 1024 is the common case. Below it, the whole
# cached-prefix argument stops applying, so the brief says so out loud.
MIN_CACHEABLE_TOKENS = 1024


def estimate_tokens(text):
    """Rough count: four characters per token.

    Deliberately approximate. For a real number, count the rendered request
    with the token counting endpoint (`messages.count_tokens`) — see
    `count_tokens` below, which does exactly that when the SDK is installed.
    Every figure derived from this function is labelled an estimate.
    """
    return max(1, len(text) // 4)


def count_tokens(text, model="claude-opus-5"):
    """Exact count via the API, or None when the SDK or a key is missing."""
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic()
        return client.messages.count_tokens(
            model=model, messages=[{"role": "user", "content": text}],
        ).input_tokens
    except Exception:
        return None


def stable_prefix(repo, depth=12):
    """Repo facts, sorted, identical for every agent on this commit.

    Sorting matters: an unordered dict rendered in a different order on the
    next request is a different prefix, and a different prefix is a cache miss.
    """
    hot = sorted(
        repo["callers"].items(), key=lambda kv: (-len(kv[1]), kv[0])
    )[:depth]

    lines = [
        f"# Repository: {Path(repo['repo']).name} at {repo['sha']}",
        "",
        "Everything below is read from the repository at that commit. It is "
        "the same for every task, so it belongs in the cached prefix.",
        "",
        "## Load-bearing files",
        "Changing one of these reaches everything that imports it.",
        "",
    ]
    for rel, callers in hot:
        info = repo["files"][rel]
        public = [s["signature"] for s in info["symbols"]
                  if not s["name"].startswith("_")][:6]
        lines.append(f"- `{rel}` — imported by {len(callers)} file(s)")
        for sig in public:
            lines.append(f"    {sig}")

    lines += ["", "## Conventions"]
    tests = sorted(r for r, i in repo["files"].items() if i["is_test"])
    if tests:
        lines.append(f"- Tests live in: {', '.join(sorted({str(Path(t).parent) for t in tests}))}")
    if repo["schema_files"]:
        lines.append(f"- Schema and migrations: {', '.join(sorted(repo['schema_files'])[:6])}")
    if repo["manifests"]:
        lines.append(f"- Dependency manifests: {', '.join(sorted(repo['manifests'])[:6])}")
    if repo["regenerated_files"]:
        lines.append(
            "- Regenerate, do not hand-merge: "
            + ", ".join(sorted(repo["regenerated_files"])[:6])
        )
    lines += [
        "",
        "## Working agreement",
        "- Other agents are working in this repository at the same time.",
        "- Stay inside the files named in your task section. If the work "
        "needs a file outside that list, say so before editing it.",
        "- Do not reformat, reorganise or 'clean up' files you were not sent to.",
    ]
    return "\n".join(lines)


def volatile_suffix(repo, forecast, risks):
    """One task's context. Changes per agent, so it goes after the cache."""
    lines = [
        f"# Task: {forecast['task']}",
        "",
        f"Forecast confidence {forecast['confidence']}. These are predictions "
        "from the repository's own structure, not instructions — if the work "
        "clearly belongs elsewhere, say so.",
        "",
        "## Start here",
    ]
    for f in forecast["files"]:
        lines.append(f"- `{f['file']}` — {f['evidence'][0]}")
        for sym in f["symbols"][:4]:
            lines.append(f"    {sym['signature']}  (line {sym['line']})")
        if f["callers"]:
            lines.append(
                f"    changing this reaches {len(f['callers'])} file(s): "
                + ", ".join(f["callers"][:4])
            )

    if forecast["tests"]:
        lines += ["", "## Tests that cover this"] + [
            f"- `{t}`" for t in forecast["tests"]
        ]

    mine = [r for r in risks if forecast["task"] in r["tasks"]]
    if mine:
        lines += ["", "## Coordination with other work in flight"]
        for r in mine:
            others = [t for t in r["tasks"] if t != forecast["task"]]
            against = f" (against: {'; '.join(others)})" if others else ""
            lines.append(f"- **{r['risk_level']}** {r['risk_type']}{against}")
            lines.append(f"  {r['recommendation']}")

    if forecast["unsupported_terms"]:
        lines += [
            "",
            "## Not grounded in this repository",
            "Nothing here matches: " + ", ".join(forecast["unsupported_terms"])
            + ". Treat that as new ground, and check before assuming an "
            "existing module handles it.",
        ]
    return "\n".join(lines)


def baseline_tokens(repo):
    """What it costs to read the repository instead of being told about it.

    Counts the code files an agent would plausibly open while orienting. It is
    an upper bound on a careful agent and a lower bound on a thorough one;
    either way it is the thing the brief replaces.
    """
    total = 0
    for rel, info in repo["files"].items():
        path = Path(repo["repo"]) / rel
        try:
            total += estimate_tokens(path.read_text(errors="replace"))
        except OSError:
            total += info["lines"] * 10
    return total


def grow_prefix(repo, measure, floor=MIN_CACHEABLE_TOKENS):
    """Widen the prefix until it is big enough to actually cache.

    A prefix under the floor is not cached at all, so a thin one is the worst
    of both worlds: full price every call, and the task section is the only
    thing that was ever going to change anyway. Rather than warn and stop,
    include more of the repo's load-bearing surface — there is usually more
    worth saying — and stop as soon as it clears, so the prefix stays honest
    rather than padded.
    """
    best = stable_prefix(repo, depth=12)
    for depth in (12, 24, 48, 96, len(repo["callers"]) or 1):
        best = stable_prefix(repo, depth=depth)
        if measure(best) >= floor:
            return best, depth
        if depth >= len(repo["callers"]):
            break
    return best, depth


def brief(repo, forecasts, risks, exact=False):
    """One cached prefix, one suffix per agent, and the arithmetic."""
    measure_for_growth = estimate_tokens
    prefix, depth = grow_prefix(repo, measure_for_growth)
    suffixes = [
        {"task": f["task"], "text": volatile_suffix(repo, f, risks)}
        for f in forecasts
    ]

    measure = (lambda t: count_tokens(t) or estimate_tokens(t)) if exact \
        else estimate_tokens
    prefix_tokens = measure(prefix)
    for s in suffixes:
        s["tokens"] = measure(s["text"])

    n = len(suffixes)
    suffix_total = sum(s["tokens"] for s in suffixes)
    explore = baseline_tokens(repo)

    # Without this: every agent reads the repo to orient, on every turn.
    # With it: the repo half is written to cache once and read back at about
    # a tenth of list price; only the task half is new each time.
    naive = explore * n
    first_call = prefix_tokens * CACHE_WRITE_MULTIPLIER + suffix_total
    warm_call = prefix_tokens * CACHE_READ_MULTIPLIER + suffix_total

    warnings = []
    if prefix_tokens < MIN_CACHEABLE_TOKENS:
        warnings.append(
            f"The prefix is {prefix_tokens} tokens even after widening it to "
            f"{depth} file(s) — everything this repo has. That is under the "
            f"~{MIN_CACHEABLE_TOKENS} minimum most models need before anything "
            "is cached, and a request under the floor succeeds at full price "
            "without saying so. The savings figures below do not apply here. "
            "The brief is still worth sending; it is just not a cache win yet."
        )

    return {
        "sha": repo["sha"],
        "prefix_depth": depth,
        "cacheable": prefix_tokens >= MIN_CACHEABLE_TOKENS,
        "warnings": warnings,
        "prefix": prefix,
        "prefix_tokens": prefix_tokens,
        "suffixes": suffixes,
        "exact": exact,
        "economics": {
            "agents": n,
            "repo_if_each_agent_reads_every_file": naive,
            "brief_first_call": round(first_call),
            "brief_per_later_call": round(warm_call),
            "saved_per_later_call": round(naive - warm_call),
            "dollars_per_later_call": round(warm_call / 1e6 * PRICE_PER_MTOK, 4),
            "dollars_if_each_agent_reads_every_file": round(naive / 1e6 * PRICE_PER_MTOK, 4),
            "note": "Estimates. Cache reads bill at roughly a tenth of input "
                    "price and writes at 1.25x; the prefix only stays warm "
                    "while its TTL holds and while no byte of it changes.",
        },
    }


def request_skeleton(data, index=0):
    """A Messages request with the cache breakpoint in the right place.

    The prefix sits in `system` with cache_control on it. The task goes in
    `messages`, after the breakpoint, where changing it costs nothing. Swap
    the task text per agent and the repo half stays cached across all of them.
    """
    return {
        "model": "claude-opus-5",
        "max_tokens": 16000,
        "system": [
            {
                "type": "text",
                "text": data["prefix"],
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {"role": "user", "content": data["suffixes"][index]["text"]}
        ],
    }
