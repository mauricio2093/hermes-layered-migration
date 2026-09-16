# The three layers

```
LAYER 1   Upstream, clean          disposable — delete and re-clone at will
   │
LAYER 2   Your code and patches    this is where customization lives
   │
LAYER 3   Config and state         config in git, state only in backups
```

The whole point is that **layer 1 can be destroyed at any moment** without
losing anything.

## The failure this prevents

The install that motivated this toolkit had one checkout serving as both
upstream *and* personal distribution. Diagnosing it turned up, in order:

| Finding | Consequence |
|---|---|
| Shallow clone, depth 15 | git cannot compute a merge base; "35 491 commits behind" is meaningless |
| On a feature branch, not `main` | `hermes update` defaults to `main` and switches branches, auto-stashing |
| A stale local `main`, not `origin/main` | that switch lands on months-old code |
| Two more branches nobody remembered | one held a commit the other did not |
| An important helper never committed | one `git stash` from disappearing |

None of these are exotic. They accumulate in any install that gets worked on.
Together they mean an unattended `hermes update` silently drops working code.

## What belongs where

**Layer 2 — a patch or a file?**

| | Kind | Lives as |
|---|---|---|
| Modifies an upstream file | patch | `patches/<group>/*.patch` |
| Standalone, yours alone | source | `tools/` |

A 199-line helper of your own is not a patch. As a versioned file it keeps
history, blame, diff and rollback. As a `.patch` it is just a copy.

**Layer 3 — config or state?**

Config is reproducible and belongs in git, with values replaced by
placeholders. State is not, and belongs only in verified backups: databases,
conversations, credentials, sessions, caches, generated documents.

## Group patches by function, never by label

Two changes shipped here under one "telegram" label turned out to be
unrelated: copy-command buttons in the platform adapter, and JSON parsing in
the command-menu config. Grouping by function let one be ported and validated
while the other waited on a design decision.

Reapply **one group at a time**, testing after each. That is the only way to
learn what upstream has since fixed for you.

## Track what was tested against what

`manifests/compatibility.yaml` records, per group, the upstream version it was
last validated against and its verdict. Without it, every upgrade re-litigates
the same questions.

Keep the labels separate. "Ported" is not "validated in production":

```yaml
port_status: applied
offline_validation: passed
live_validation: pending
```

One collapsed `status: applied` hides which of those three is actually true.
