# Hermes Layered Migration

Tooling and doctrine for keeping a heavily customized
[Hermes Agent](https://github.com/NousResearch/hermes-agent) install
upgradable — without losing your own work and without trusting a backup you
have never restored.

It exists because an install that had been worked on for months could no
longer be updated safely, and finding out why took a full audit.

---

## The problem

Run `hermes update` on a customized install and one of two things happens: it
works, or it quietly discards code you wrote. Which one you get depends on
facts about your checkout that nothing surfaces until it is too late.

The install this came from had all of these at once:

- a **shallow clone**, so git could not compute a merge base and reported
  "35 491 commits behind" — a meaningless number
- checked out on a **feature branch**, while `hermes update` targets `main`
  and switches branches, auto-stashing on the way
- a **stale local `main`** that was not `origin/main`, so that switch would
  have landed on months-old code
- **two more local branches** nobody remembered, one holding a commit the
  other did not
- an important helper **never committed**, one `git stash` from vanishing
- **no backup had ever been created**, despite config asking for one

Every one of these is ordinary. They accumulate. Together they mean an
unattended update silently drops working code.

---

## The approach

**Three layers, and the first one is disposable.**

```
LAYER 1   Upstream, clean          delete and re-clone at will
   │
LAYER 2   Your code and patches    where customization lives
   │
LAYER 3   Config and state         config in git, state only in backups
```

Once upstream is disposable, upgrading stops being a negotiation. You re-clone
and reapply, instead of rebasing a checkout whose history no longer resembles
anyone's.

See [docs/layers.md](docs/layers.md) for what belongs in each layer, when a
change should be a patch versus a file, and why patches are grouped by
function rather than by whatever label they shipped under.

---

## What's here

| | |
|---|---|
| [`scripts/backup-hermes-home.sh`](scripts/backup-hermes-home.sh) | Backup whose success is proven by restoring it |
| [`scripts/scan-secrets.sh`](scripts/scan-secrets.sh) | Secret scan across full git history |
| [`docs/verification.md`](docs/verification.md) | What "verified" means, and the two backups that lied |
| [`docs/layers.md`](docs/layers.md) | The three-layer model |
| [`docs/secret-scanning.md`](docs/secret-scanning.md) | Scanning before a remote exists |
| [`docs/runbook-live-cutover.md`](docs/runbook-live-cutover.md) | Swapping a live gateway, with rollback |
| [`hermes_layer2/`](hermes_layer2/) | The layer-2 code itself: schema versioning and adaptive command ranking |
| [`manifests/compatibility.yaml`](manifests/compatibility.yaml) | Which group was validated against which version |
| [`patches/telegram/`](patches/telegram/) | A worked example: two features ported to a 35k-commit-newer upstream |

---

## The one rule

> **Verified means restored and validated by an independent mechanism — never
> read back by the same tool that produced it.**

This is not a principle. It is a scar.

A `git bundle` was created without error. `git bundle verify` reported *"The
bundle records a complete history."* Cloning from it failed outright: the
source was shallow, so the bundle referenced objects that did not exist. **The
tool that wrote the backup could not tell that the backup was broken.**

Then the backup script made the same class of mistake in a different costume:
it copied every database consistently, verified each copy, wrote a checksum
manifest — and deleted the copies before archiving, shipping the hot-copied
originals instead. It reported `backup_verified: true`.

So `backup_verified` is now **derived**, never assigned:

```json
{
  "created": true,
  "archive_integrity": true,
  "database_integrity": true,
  "manifest_integrity": true,
  "restore_verified": true
}
```

All five, or the script exits non-zero. And `restore_verified` extracts the
archive and checks **contents** — file counts per directory, permission modes
on secrets, `integrity_check` on every database, and that no hot-copied
database sneaked in.

The counts matter most. A structurally perfect archive that a bad glob left
half empty extracts cleanly and passes every checksum. Only a count catches
it — and it has, twice, including a sixth database nobody knew existed.

Full account in [docs/verification.md](docs/verification.md).

---

## What layer 2 looks like in practice

[`hermes_layer2/`](hermes_layer2/) is the worked example: it adds a feature to
Hermes while touching almost nothing of it.

**Its own schema line.** Upstream owns `schema_version`; taking a number from it
would mean that the day upstream ships its own next version, two different
schemas share one number. So layer 2 versions itself, per component, in the same
database — no second database, no loose JSON file.

**Migrations that cannot half-apply.** DDL and the version bump commit or roll
back together. Otherwise a crash between them leaves a table whose recorded
version says it does not exist.

**An adaptive slash menu**, as the first consumer. Commands are ordered by
frequency *and* recency: the score decays exponentially with a 30-day half-life,
and — the part that is easy to get wrong — **it decays on read as well as on
write**. Decaying only on use leaves an abandoned command frozen at its old
score forever, because nothing ever touches its row again. A command used 100
times and then ignored for six months reads as ~1.6, and loses to one used 41
times last week.

**Learning is fast; publishing is slow.** On a chat platform the menu is set
through an API, so the trigger is the rendered payload — the exact commands and
descriptions in order — never the scores behind it. A measured burst: 100
executions produce 100 writes, 10 recomputations and **one** API call.

**Fail-open everywhere.** An adaptive menu is a convenience. A missing or
corrupt database returns the original order and records nothing; it must never
be the reason a command does not run.

## Automation, and where it must stop

The intended shape is a nightly job that does all the tedious work and then
**refuses to touch upstream**:

```
PRE-FLIGHT → DISCOVERY → SAFE UPDATES → report → pending_approval
```

It verifies backups, fetches, diffs versions, updates the components that are
genuinely reversible, and stops — leaving a report to approve in the morning.

Upstream itself is never mutated unattended. A serious update means reading
what actually changed, and a cron job cannot do that. The automation removes
the toil, not the judgment.

Two things resist automation harder than they look, and this toolkit treats
both as approval-gated rather than "reversible":

- **System packages.** A package cache is not a rollback. It can be cleared,
  it may not hold every previous version, and dependencies do not always walk
  backwards cleanly. Simulate, report, approve.
- **Python environments.** `pip freeze` is an excellent photograph and a poor
  restore. Build a fresh environment alongside, health-check it, and swap
  atomically — then rollback is repointing a symlink.

---

## Worked example: porting across 35 000 commits

Two changes shipped under one "telegram" label. Splitting them by function
showed they were unrelated, and treating them separately paid off immediately.

**First, read intent — never force the patch.** `git am` conflicted, so the
patches were never applied blind. Instead each change was tested against the
new upstream with `git apply --reverse --check`:

| Verdict | Meaning | Result |
|---|---|---|
| reverses cleanly | already upstream | 3 patches dropped |
| applies cleanly | missing, lands clean | none |
| conflicts | code moved around it | the rest |

Three patches were deleted rather than ported. That is the whole point of
checking.

**Upstream's refactor made the port smaller.** The adapter had shrunk 35%, and
two duplicated send paths had merged into one helper — so five hook points
became four, and the final diff was **+24 −3**. A port is not always a tax.

**One change hit a genuine design conflict.** Upstream had started handling
the same input, but with different semantics: a string meant *one command
name*, so a serialized list became a single invented name — a plausible wrong
value rather than a visible error. The fix was a deliberately narrow decoder:
only a JSON array of strings is adopted, and every other input falls through
to upstream's untouched branch. Upstream's own test still passes, unmodified.

That is the rule worth keeping: **add a compatibility layer, never overwrite
upstream's decision.**

Result: 695 → 733 tests passing, zero regressions, four small commits.

---

## Honest limits

- **Live validation is pending.** The port passes offline. It has not run
  against real traffic, and the manifest says so in its own field rather than
  hiding behind a single `applied`.
- **The full upstream test suite was never run here.** 1 145 test files do not
  fit on a 6 GB machine that is also running the gateway; it dies the same way
  on a clean `main`, so it is a hardware limit and not a regression — but it
  is unproven, and recorded as such.
- **This is one install's experience.** The doctrine generalizes; the file
  counts and database names in the scripts do not. Treat them as a baseline to
  review, which is exactly what makes them useful.

---

## License

MIT — see [LICENSE](LICENSE). The patches carry context lines from Hermes
Agent (MIT, © 2025 Nous Research); see [NOTICE](NOTICE).
