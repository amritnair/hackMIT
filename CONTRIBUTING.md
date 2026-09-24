# Contributing

## Running it

```
pip install -e .
python tests/test_prophecy.py
```

The suite builds throwaway git repositories in temp directories and drives
the real thing against them: the MCP server over its own wire protocol, a
replay of a conflicting merge, the auth gate from outside. It takes a couple
of minutes and needs no fixtures, no services and no network.

## House rules

**No dependencies.** Python 3.10 and the standard library. This runs inside
other people's repositories, and a tool that warns about coupling should not
arrive with four hundred transitive packages. `anthropic` and `openai` are
imported behind a guard in `llm.py` and are never on the MCP path.

**Every warning carries its evidence.** A score with no reason behind it is
noise, and the first bad one teaches people to skim the list. If you add a
detector, it says what it saw.

**Say what is not known.** The analysis has an uncertainty list for a
reason: TypeScript is read with pattern matching, the import index is a
suffix match, nothing here observes runtime. Adding to those lists is not a
failure, quietly rounding them up is.

**Leave a check behind.** Anything with a branch in it gets an assertion in
`tests/test_prophecy.py`. Then break the thing on purpose and confirm the
test fails — a test that passes either way is worse than none, because it
reads as coverage.

## Layout

```
prophecy/        the package: scan, predict, risk, mcp, server, dashboard
tools/           deploying and publishing, not part of the package
tests/           one suite, no framework
docs/            the published site, generated — do not edit by hand
.github/         build the image, publish the site, run the tests
```

`docs/` is written by `tools/export_static.py` and committed by CI. Editing
it directly means your change is gone at the next push.
