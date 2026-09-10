# Actions Pinger — fix GitHub scheduler under-delivery (15-min workflows)

**Status: ROLLED BACK to local crontab 2026-09-10** — the cron-job.org
cloud pinger delivered ZERO dispatches from 17:00 (2026-09-09, local
decommission) until 00:04 (2026-09-10, rollback): no workflow_dispatch
runs exist in that 7-hour window, the hourly heartbeat went silent, and
no failure alerts arrived (silent free-tier failure — suspected suspended
jobs or rejected auth). The LOCAL crontab pinger below is re-installed
and verified dispatching (204s). The cron-job.org jobs should be deleted
or repaired from the dashboard at console.cron-job.org; keep the local
pinger as the primary until a cloud alternative is actually verified.

## Problem (measured, STATUS_REPORT.md §16)

GitHub's scheduler collapses the 15-min crons (`trade`, `chat`) to ~6-8%
of requested runs: median chat reply ~92 min late, 183-min average gaps
between runs. Queue time 0s, failures 0 — scheduled runs simply never
get created. Chronic, not episodic.

## Implemented fix: local crontab pinger (REINSTALLED 2026-09-10)

This machine's cron fires authenticated `workflow_dispatch` POSTs every
15 minutes for `chat` and `trade`. Dispatch-created runs bypass the
broken scheduler entirely. Verified again on rollback: `HTTP 204` →
runs created → `completed/success`, heartbeat resumed within minutes.

### Components (all under `~/.config/trading-pinger/`)

| File | Role |
|---|---|
| `pinger.sh` | POSTs dispatches for chat + trade; freshness-checks agent + futures workflows (>100 min stale → dispatch); logs HTTP codes to `pinger.log` |
| `refresh_token.sh` | weekly re-cache of the gh CLI token (Sundays 04:00) |
| `gh_token` | cached gh OAuth token (mode 600; gh itself refreshes on use) |

### Crontab (REINSTALLED 2026-09-10)

Active again after the cron-job.org failure. Verify any time:
`crontab -l` — expect the two lines under Rollback below.

## Current setup: LOCAL crontab pinger (since 2026-09-10 rollback)

`*/15 * * * *` fires `pinger.sh`, which POSTs dispatches for chat +
trade and freshness-checks agent + futures workflows (>100 min stale →
dispatch), logging HTTP codes to `pinger.log`. Auth: the cached gh OAuth
token (`gh_token`, mode 600, refreshed weekly by `refresh_token.sh`).

The cron-job.org experiment (2026-09-09 → 2026-09-10) failed silently:
zero dispatches in 7 hours, no failure alerts. Delete or repair those
jobs at console.cron-job.org if you want a cloud backup later — until
one is VERIFIED delivering, the local pinger is the sole reliable
dispatch source.

**Host dependency:** this machine must stay on for the pinger to fire.
If it sleeps/shuts down, 15-min workflows degrade to GitHub's native
scheduler (~6-8% delivery) and the hourly heartbeat falls back to the
agent workflow's native hourly cron (usually reliable).

### How it interacts with native schedules

Native crons (`trade` 4,19,34,49 / `chat` 9,24,39,54) remain in place
as best-effort backup. Interleaved offsets mean no systematic collision;
GitHub's per-workflow concurrency groups serialize any accidental
overlap (cancel-in-progress: false — second run queues, never lost).
Chat is idempotent (cursor in meta `discord_chat_last_seen`), trade is
idempotent (edge-triggered cross state).

### Verify / operate

```bash
gh run list --workflow trade.yml --limit 3   # expect workflow_dispatch runs
gh run list --workflow chat.yml --limit 3    # every ~15 min
```

Pain-meter (`actions_health()` in the daily report) reads `ok` for
trade/chat — it measures exactly this.

### Staleness net for hourly workflows (local pinger era, 2026-09-09)

The LOCAL pinger also checked the LAST RUN of `agent`/`futures` and
dispatched when >100 min stale. The cloud pinger does blind POSTs and
does not replicate this — acceptable because GitHub delivers hourly
schedules reliably and the agent-cycle heartbeat fallback
(bot/trader.py) posts the hourly heartbeat even when 15-min trade
cycles never ran. If hourly delivery ever degrades, add two more
cron-job.org jobs pointing at agent.yml/futures.yml dispatch URLs.

### Known limitation — third-party dependency

cron-job.org could change its free tier or shut down. Watch for its
failure-alert emails; if they ever stop arriving, check the dashboard.
The repo's own native schedules remain as a reduced-rate fallback, and
the rollback below restores the local pinger in minutes.

## Rollback (currently ACTIVE — this IS the live setup)

`crontab -l` should show:
```
*/15 * * * * /home/brightkwame/.config/trading-pinger/pinger.sh
0 4 * * 0   /home/brightkwame/.config/trading-pinger/refresh_token.sh
```
Nothing in the repo depends on the pinger; native schedules continue
at their reduced rate either way.

## Future note (real-money day)

Guaranteed 15-min delivery from this setup is fine for paper trading.
If/when graduating to real money on Binance, the runner must move to a
non-US host anyway (geo) — see STATUS_REPORT.md §16 venue table; that
migration hosts the runner itself and this pinger becomes redundant
(keep it for the still-Actions-hosted tiers if any remain).
