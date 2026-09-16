# Runbook — live gateway cutover

Run this **only while watching the messaging client**. It is mechanical: if
anything fails, roll back and analyze afterwards. **Never fix anything hot.**

## Why there is no parallel test

Telegram allows exactly one `getUpdates` poller per bot token. A second
gateway on the same token produces `Conflict` and the two knock each other
over. The cutover is therefore a real service interruption, however short.

## Before

- [ ] Backup A current, `backup_verified: true`
- [ ] Save the current unit so rollback is a copy, not a reconstruction:

      systemctl --user cat hermes-gateway.service > rescue/unit-previous.service

## Cutover

1. `systemctl --user stop hermes-gateway.service`
2. Repoint the unit at the clean clone:
   - `ExecStart` → `<clean-clone>/.venv/bin/python -m hermes_cli.main gateway run`
   - `VIRTUAL_ENV` → `<clean-clone>/.venv`
   - `HERMES_HOME` unchanged — state stays where it is
3. `systemctl --user daemon-reload`
4. `systemctl --user start hermes-gateway.service`
5. Health check by **response**, not by `is-active`. An active service
   returning errors is not a success.

## Acceptance

| # | Send | Expect |
|---|---|---|
| 1 | an ordinary message | identical to before, no buttons |
| 2 | a reply containing a ```bash block | a copy-command button |
| 3 | a flow that already uses a keyboard | original keyboard intact, copy rows appended below |
| 4 | a JSON priority list via `config set` | menu ordered correctly, not one invented name |

Test 3 is the regression that matters. The feature appends rows to whatever
keyboard the caller already set; if it replaced them instead, approval flows
would break silently.

## Rollback

    systemctl --user stop hermes-gateway.service
    cp rescue/unit-previous.service <unit path>
    systemctl --user daemon-reload
    systemctl --user start hermes-gateway.service

Keep the logs before retrying anything.

## Keep the old checkout

Do not delete it on cutover day. It is the last net, it costs only disk, and
nothing about a green test suite proves a week of real traffic. Retire it once
the new one has been boring for several days.
