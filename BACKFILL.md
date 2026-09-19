# grading the forecast against real merge history

`mergemind backfill` replays merges that already happened. For each two-parent
merge commit it finds the point where the two sides diverged, scans the repo
as it was *at that commit*, forecasts from the two sets of changes, and then
re-runs the merge in a scratch worktree to see what git makes of it. The
forecast never sees the merge commit, so it cannot work backwards from the
answer.

Run against [pallets/flask](https://github.com/pallets/flask), 250 merge
commits, of which 24 produced text conflicts.

## the first result was wrong, and usefully so

The first run said this:

```
  high      214 flagged    13 conflicted    6.1%
  medium    302 flagged    60 conflicted   19.9%
```

Files the risk engine shouted about conflicted at a third the rate of files it
merely mentioned. Taken at face value, the score was worse than useless — it
was backwards.

It was not backwards. Breaking the same numbers down by file type:

```
  conflicted files by extension:
    .yaml 20   .txt 16   .py 13   .lock 9   .rst 6   .toml 6

  high   flagged:  .py 214
  medium flagged:  .py 152   .yaml 53   .txt 29   .rst 25   .toml 18   .lock 9
```

Every `high` was a source file. The `medium` bucket was half lockfiles, CI
workflow yaml and changelogs — files that two branches collide in almost every
time, for reasons that have nothing to do with how risky the work was. Two
people both adding a line to `CHANGES.rst` is a conflict. It is not a
coordination problem.

Comparing like with like:

```
  high    code       214 flagged   13 conflicted    6.1%
  medium  code       152 flagged    0 conflicted    0.0%
  medium  non-code   150 flagged   60 conflicted   40.0%
```

Among source files the ordering is exactly right: every text conflict in a
code file landed in a file the engine had marked `high`, and nothing marked
`medium` conflicted at all.

## what got fixed because of it

Two things, one in each direction.

**The metric was comparing unlike things.** `report()` now splits code from
non-code before comparing levels, so a changelog that conflicts every single
time can no longer make the scoring look inverted.

**The detector was giving bad advice.** Generated and high-churn files —
lockfiles, `.github/workflows/*`, changelogs — now get their own risk type
instead of being lumped in as an ordinary shared file. At a 40% conflict rate
they are the single most collision-prone category in the repo, and the right
advice for them is not "agree who owns this file". It is: take one side, then
regenerate or re-append after rebasing.

## after the fix

Re-running the same 250 merges with both changes in place:

```
  high   (code)      214 flagged   13 conflicted    6.1%
  medium (code)      152 flagged    0 conflicted    0.0%
  high   (non-code)   61 flagged   28 conflicted   45.9%
  medium (non-code)   89 flagged   32 conflicted   36.0%

  250 merges replayed, 24 of them conflicted
  3042 files touched across both sides, 516 flagged
  73 files really conflicted — 73 flagged, 0 missed
  median warning lead time: 26.5 hours before the merge
```

Among source files the ordering holds and the separation is total. The new
`regenerated_file_overlap` type accounts for most of the non-code `high`
bucket, and those files conflict 45.9% of the time — which is the point of
giving them a category of their own rather than burying them among ordinary
shared files.

The lead time is the other number worth keeping: 26.5 hours is the median gap
between two branches diverging and the merge that brought them back together.
That is how much earlier this could have said something.

## what these numbers do not show

**Recall on text conflicts is not an achievement.** Every conflicting file was
flagged in advance, in both runs. That is arithmetic, not skill: a text
conflict requires both sides to edit the same file, which is the exact
condition that makes this tool fire. Missing one would indicate a bug. The
report says so rather than printing 100% and moving on.

**Text conflicts are the wrong outcome to grade against.** Git complaining is
the easy case — the case that would have been caught anyway, at merge time,
by anyone. The failures this project exists for are the ones that merge
cleanly and break at runtime, and nothing in this run measures those. Doing
that means merging cleanly and then running the tests, which `verify --test`
already supports and the backfill does not, because a historical test suite
needs that commit's dependencies installed.

**One repo is one repo.** Flask is a mature Python project with small, tidy
pull requests. A monorepo with long-lived branches would look different, and
nothing here predicts how.

## reproducing

```bash
git clone https://github.com/pallets/flask.git
mergemind -C flask backfill --limit 250
```

About six minutes. Every intermediate merge runs in a temporary worktree and
is torn down afterwards; the clone is left as it was found.
