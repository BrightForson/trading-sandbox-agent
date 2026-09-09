# Actions Pinger — fix GitHub scheduler under-delivery (15-min workflows)

**Status: MIGRATED to cron-job.org 2026-09-09** (cloud pinger, always-on —
the local crontab pinger was decommissioned the same day after cloud
dispatches were verified landing every 15 min. Local pinger files remain
at `~/.config/trading-pinger/` for reference/rollback.)

## Problem (measured, STATUS_REPORT.md §16)

GitHub's scheduler collapses the 15-min crons (`trade`, `chat`) to ~6-8%
of requested runs: median chat reply ~92 min late, 183-min average gaps
between runs. Queue time 0s, failures 0 — scheduled runs simply never
get created. Chronic, not episodic.

## Implemented fix: local crontab pinger

This machine's cron fires authenticated `workflow_dispatch` POSTs every
15 minutes for `chat` and `trade`. Dispatch-created runs bypass the
broken scheduler entirely. First pings verified: `HTTP 204` → runs
created → `completed/success`.

### Components (all under `~/.config/trading-pinger/`)

| File | Role |
|---|---|
| `pinger.sh` | POSTs dispatches for chat + trade; freshness-checks agent + futures workflows (>100 min stale → dispatch); logs HTTP codes to `pinger.log` |
| `refresh_token.sh` | weekly re-cache of the gh CLI token (Sundays 04:00) |
| `gh_token` | cached gh OAuth token (mode 600; gh itself refreshes on use) |

### Crontab (DECOMMISSIONED 2026-09-09)

The local crontab lines were removed when the cloud pinger took over.
Files kept at `~/.config/trading-pinger/` (pinger.sh with the
agent+futures staleness net, refresh_token.sh, gh_token) for rollback.

## Current setup: cron-job.org cloud pinger (since 2026-09-09)

Two always-on POST jobs at console.cron-job.org, every 15 min:

| Job | URL |
|---|---|
| `ping-trade` | `https://api.github.com/repos/BrightForson/trading-sandbox-agent/actions/workflows/trade.yml/dispatches` |
| `ping-chat` | `https://api.github.com/repos/BrightForson/trading-sandbox-agent/actions/workflows/chat.yml/dispatches` |

Headers: `Authorization: Bearer <fine-grained PAT>`,
`Accept: application/vnd.github+json`; body `{"ref":"main"}` as
application/json; 30s timeout; failure alerts ON. The PAT is fine-grained,
repo-scoped to this repo only, Actions: Read and write only, 90-day
expiry (**rotate ~2026-09-08 + 90d = 2026-12-07** — the jobs will 401
silently after expiry; cron-job.org failure alerts catch it).

Verified working 2026-09-09: dispatches land every 15 min, all runs
`workflow_dispatch` + `completed/success`.

Trade-offs accepted vs the local pinger:
- cron-job.org does dumb blind POSTs — the agent/futures staleness net
  (dispatch only if >100 min stale) is NOT replicated. That's OK:
  GitHub delivers hourly schedules reliably, and the agent-cycle
  heartbeat fallback (bot/trader.py) already covers missed heartbeats.
- The PAT lives in a third-party dashboard; blast radius is capped by
  fine-grained scoping (one repo, Actions only, 90d).

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

## Rollback

`crontab -e`, re-add the two lines from `~/.config/trading-pinger/`:
```
*/15 * * * * /home/brightkwame/.config/trading-pinger/pinger.sh
0 4 * * 0   /home/brightkwame/.config/trading-pinger/refresh_token.sh
```
Then disable both cron-job.org jobs. Nothing in the repo depends on
either pinger; native schedules continue at their reduced rate either way.

## Future note (real-money day)

Guaranteed 15-min delivery from this setup is fine for paper trading.
If/when graduating to real money on Binance, the runner must move to a
non-US host anyway (geo) — see STATUS_REPORT.md §16 venue table; that
migration hosts the runner itself and this pinger becomes redundant
(keep it for the still-Actions-hosted tiers if any remain).
