"""Optional model-backed prediction, grounded in what the repo actually has.

The lexical matcher has a hard ceiling: it cannot connect "throttle incoming
requests" to a function called `rate_limit`, because the two share no letters.
A model closes that gap. It also opens a new one — a model asked where work
belongs will happily name a plausible file that does not exist.

So the model never gets to name a file. It is handed the repository's real
inventory and asked to choose from it, and anything it returns that is not in
that inventory is dropped and reported as dropped. Model-derived hits are
marked `source: "model"` so they never get mistaken for repository evidence.

Off by default. Nothing here runs unless somebody passes --llm.
"""

import json
import os
import re

from .text import plural

SYSTEM = (
    "You map a described piece of engineering work onto files that already "
    "exist in a repository.\n\n"
    "Rules:\n"
    "- Choose only from the inventory given to you. Never invent a path.\n"
    "- Prefer the file that would actually change over files that merely "
    "mention the topic.\n"
    "- If nothing in the inventory fits, return an empty list. That is a "
    "useful answer, not a failure.\n"
    "- Reply with JSON only: "
    '{"files":[{"path":"...","why":"one short clause","confidence":0.0}]}'
)


class NoProvider(RuntimeError):
    """No model configured, or its SDK is not installed."""


class Anthropic:
    """Default. The inventory goes in a cached system block: it is the same
    for every task against a commit, so it is paid for roughly once."""

    name = "anthropic"
    default_model = "claude-opus-5"

    def __init__(self, model=None):
        try:
            import anthropic
        except ImportError as exc:
            raise NoProvider("pip install anthropic") from exc
        self.model = model or os.environ.get("MERGEMIND_MODEL", self.default_model)
        self._client = anthropic.Anthropic()

    def complete(self, stable, volatile, max_tokens=1500, system=None):
        message = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[
                {"type": "text", "text": system or SYSTEM},
                {
                    "type": "text",
                    "text": stable,
                    # The inventory is the expensive, unchanging half. Cache
                    # it here and every later task against this commit reads
                    # it back instead of resending it.
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[{"role": "user", "content": volatile}],
        )
        return "".join(b.text for b in message.content if b.type == "text")


class OpenAI:
    """Same contract, different vendor. No caching hook — the request is sent
    whole every time, so the token arithmetic in `brief` does not apply."""

    name = "openai"
    default_model = "gpt-4o"

    def __init__(self, model=None):
        try:
            from openai import OpenAI as Client
        except ImportError as exc:
            raise NoProvider("pip install openai") from exc
        self.model = model or os.environ.get("MERGEMIND_MODEL", self.default_model)
        self._client = Client()

    def complete(self, stable, volatile, max_tokens=1500, system=None):
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": (system or SYSTEM) + "\n\n" + stable},
                {"role": "user", "content": volatile},
            ],
        )
        return response.choices[0].message.content or ""


PROVIDERS = {"anthropic": Anthropic, "openai": OpenAI}


def get_provider(name=None):
    """Named provider, or whichever one the environment is set up for."""
    name = name or os.environ.get("MERGEMIND_LLM")
    if name:
        if name not in PROVIDERS:
            raise NoProvider(f"unknown provider {name}; try {', '.join(PROVIDERS)}")
        return PROVIDERS[name]()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return Anthropic()
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAI()
    raise NoProvider(
        "No model configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, or "
        "pick one with MERGEMIND_LLM=anthropic|openai. Everything else in "
        "prophecy works without it."
    )


def inventory(repo, limit=400):
    """Every file the model is allowed to name, with its symbols.

    Sorted, so the same commit renders the same bytes and the cached prefix
    stays warm.
    """
    lines = [f"Repository inventory at {repo['sha']}:"]
    for rel in sorted(repo["files"])[:limit]:
        info = repo["files"][rel]
        names = ", ".join(s["name"] for s in info["symbols"][:12])
        flag = " [test]" if info["is_test"] else ""
        lines.append(f"{rel}{flag}: {names}" if names else f"{rel}{flag}")
    return "\n".join(lines)


def parse(text):
    """Pull the JSON out, whatever the model wrapped it in."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {"files": []}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"files": []}


def semantic_predict(repo, task, provider=None):
    """Ask a model where the work lands, then check every answer against the
    repository before believing any of it."""
    provider = provider or get_provider()
    raw = provider.complete(
        inventory(repo),
        f"Work to be done: {task}\n\nWhich of those files change?",
    )
    proposed = parse(raw).get("files") or []

    grounded, invented = [], []
    for item in proposed:
        path = (item or {}).get("path", "")
        if path in repo["files"]:
            grounded.append({
                "file": path,
                "score": round(float(item.get("confidence") or 0.5) * 6, 2),
                "symbols": repo["files"][path]["symbols"][:5],
                "evidence": [
                    f"{provider.name} ({provider.model}): "
                    f"{item.get('why', 'no reason given')}"
                ],
                "is_test": repo["files"][path]["is_test"],
                "callers": repo["callers"].get(path, []),
                "source": "model",
            })
        elif path:
            invented.append(path)

    return {
        "files": grounded,
        "invented": invented,
        "provider": provider.name,
        "model": provider.model,
    }


def merge_forecasts(lexical, semantic):
    """Lexical evidence first, model inference after, never silently blended.

    A file both agree on keeps its repository evidence and gains the model's
    reasoning as a second line. A file only the model found is kept, marked,
    and ranked below anything the repository itself pointed at.
    """
    by_path = {f["file"]: dict(f, source=f.get("source", "repo"))
               for f in lexical["files"]}
    for hit in semantic["files"]:
        if hit["file"] in by_path:
            existing = by_path[hit["file"]]
            existing["evidence"] = existing["evidence"][:3] + hit["evidence"]
            existing["source"] = "repo+model"
            existing["score"] = round(existing["score"] + 1.0, 2)
        else:
            by_path[hit["file"]] = hit

    merged = sorted(by_path.values(),
                    key=lambda f: (f["source"] == "model", -f["score"], f["file"]))
    out = dict(lexical)
    out["files"] = merged[:10]
    out["provider"] = semantic["provider"]
    out["model"] = semantic["model"]
    out["model_invented"] = semantic["invented"]
    # Model-only hits are inference. Saying so is the whole point.
    out["grounding"] = (
        f"{sum(1 for f in merged if f['source'] == 'repo')} from repository "
        f"evidence, {sum(1 for f in merged if f['source'] == 'repo+model')} "
        f"confirmed by both, {sum(1 for f in merged if f['source'] == 'model')} "
        f"from the model alone"
        + (f", {plural(len(semantic['invented']), 'invented path')} dropped"
           if semantic["invented"] else "")
    )
    return out


NARRATOR = (
    "You brief an engineering team on what is happening in their repository "
    "right now. You are given facts that were computed from the repository "
    "and from live agent sessions.\n\n"
    "Rules:\n"
    "- Use only the facts given. Do not invent files, people, numbers or "
    "causes, and do not soften or inflate what is there.\n"
    "- Lead with what matters to whoever is about to start work.\n"
    "- Plain sentences, no headings, no bullet points, no preamble. Four "
    "short paragraphs at most.\n"
    "- These are predictions from structure, not observed failures. Say so "
    "where it matters rather than implying certainty."
)


def narrate(facts, provider=None):
    """Have a model write the briefing from facts already computed.

    Opt-in, and never the only copy: the deterministic summary is kept and
    shown as the fallback, because prose generated from facts can still drift
    from them in a way a rule cannot.
    """
    provider = provider or get_provider()
    body = json.dumps(facts, indent=2, default=str)[:12000]
    text = provider.complete(
        "", f"Facts:\n{body}\n\nBrief the team.",
        max_tokens=700, system=NARRATOR,
    )
    return [line.strip() for line in text.split("\n") if line.strip()]
