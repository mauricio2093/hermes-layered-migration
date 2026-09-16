# Secret scanning before any remote exists

`scripts/scan-secrets.sh <repo>` walks **every blob in git history**, not the
working tree. A secret deleted three commits ago is still published when the
repository is.

    scan-secrets.sh /path/to/repo               # full history (default)
    scan-secrets.sh /path/to/repo --tree-only   # current tree only

Exit status is non-zero when anything is found.

## Validate the detector before trusting a clean result

A scanner that finds nothing and a scanner that is broken produce identical
output. Seed a throwaway repository with known secrets, commit them, delete
the file, commit again, and confirm history mode still reports them.

Doing exactly that caught a real defect here: the first version found **one of
five** planted secrets. The benign-value filter used a ±40 character window
around each match, which crossed line boundaries, so an `AKIA…EXAMPLE` on one
line silenced genuine secrets on its neighbours. The filter now evaluates only
the line the match sits on.

False negatives are the dangerous direction. A false positive costs a minute.

## What it looks for

Private keys, Telegram bot tokens, OpenAI/Anthropic keys, Slack tokens, GitHub
PATs, AWS access keys, Google API keys, credential assignments, and URLs with
inline credentials. Obvious placeholders (`your-…`, `changeme`, `<token>`,
`EXAMPLE`) are suppressed on their own line.

## Secrets are not the only problem

Grep separately for **internal identifiers**: hostnames, VPN addresses,
hardware inventory, remote access patterns, internal URLs.

This is what stopped one repository from being published here. Its history
carried no credentials at all, but every commit contained operational
documents with a VPN address, a real hostname and a hardware inventory. The
fix was a clean snapshot with a new history, not a rewrite of the old one —
the data was in every commit.

Generated files deserve the same suspicion as configuration. A directory of
machine-written notes held network topology and Wi-Fi credential terms. Version
the **generators**, never what they generate.
