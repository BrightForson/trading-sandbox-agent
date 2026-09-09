# Actions Pinger — fix GitHub scheduler under-delivery (15-min workflows)

**Status: LIVE since 2026-09-07T23:24Z** (local crontab pinger)

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

### Crontab (installed)

```
*/15 * * * * /home/brightkwame/.config/trading-pinger/pinger.sh
0 4 * * 0   /home/brightkwame/.config/trading-pinger/refresh_token.sh
```

### How it interacts with native schedules

Native crons (`trade` 4,19,34,49 / `chat` 9,24,39,54) remain in place
as best-effort backup. Interleaved offsets mean no systematic collision;
GitHub's per-workflow concurrency groups serialize any accidental
overlap (cancel-in-progress: false — second run queues, never lost).
Chat is idempotent (cursor in meta `discord_chat_last_seen`), trade is
idempotent (edge-triggered cross state).

### Verify / operate

```bash
tail ~/.config/trading-pinger/pinger.log        # expect "HTTP 204" lines
crontab -l                                     # two pinger lines
gh api repos/BrightForson/trading-sandbox-agent/actions/runs?per_page=6 \
  --jq '.workflow_runs[] | "\(.name) \(.event) \(.conclusion)"'
```

Pain-meter (`actions_health()` in the daily report) now reads `ok` for
trade/chat — it measures exactly this.

### Staleness net for hourly workflows (added 2026-09-09)

The pinger now also checks the LAST RUN of `agent` and `futures` (hourly
workflows GitHub delivers reliably). If either is >100 min stale it
dispatches them too. This plus the agent-cycle heartbeat fallback means
an asleep laptop can no longer cause a missing-hour heartbeat — the
hourly agent workflow posts it even when 15-min trade cycles never ran.

### Known limitation — this machine is the cron host

Pings only fire while this machine is awake and online. If the trading
stack later moves to the Oracle Cloud / GCP free-tier VPS (planned),
copy `~/.config/trading-pinger/` there and install the same crontab —
an always-on VPS cron host fully solves the asleep-laptop problem.
Alternative cloud option: migrate to cron-job.org (always-on, free):

- Sign up console.cron-job.org; create two POST jobs to
  `https://api.github.com/repos/BrightForson/trading-sandbox-agent/actions/workflows/{chat,trade}.yml/dispatches`
  with headers `Authorization: Bearer <fine-grained PAT, Actions:write>`,
  `Content-Type: application/json`, body `{"ref":"main"}`, every 15 min,
  failure alerts on. PAT: github.com → Settings → Developer settings →
  Fine-grained tokens → repo-scoped to `trading-sandbox-agent`,
  Actions: Read and write only, 90-day expiry.

Then remove the local crontab (`crontab -e`, delete the two lines).

## Rollback

`crontab -e`, delete the two pinger lines. Nothing in the repo depends
on the pinger; native schedules continue at their reduced rate.

## Future note (real-money day)

Guaranteed 15-min delivery from this setup is fine for paper trading.
If/when graduating to real money on Binance, the runner must move to a
non-US host anyway (geo) — see STATUS_REPORT.md §16 venue table; that
migration obsoletes this pinger entirely.
