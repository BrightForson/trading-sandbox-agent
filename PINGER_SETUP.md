# Cron Pinger Migration Plan — fix GitHub Actions scheduler under-delivery

**Date:** 2026-09-08 · **Status:** ready to execute
**Decision owner:** BrightForson

## Problem (measured, STATUS_REPORT.md §16)

GitHub's scheduler collapses our 15-min crons to ~6-8% of requested runs:
chat replies median ~92 min late, trade cycles avg 183-min gaps. Queue
time 0s, failures 0 — runs just never get *created*. Chronic since Sep 5.

## Fix: cron-job.org fires `workflow_dispatch` instead of relying on schedules

Free, no card, 15+ years old, 60 runs/hour max, execution history +
failure notifications. Dispatch-created runs bypass the scheduler
entirely — they attack the exact root cause: runs not being created.

## Prerequisites (owner, ~5 min)

1. **Create a fine-grained PAT**: github.com → Settings → Developer
   settings → Fine-grained tokens → Generate new
   - Token name: `cron-pinger`
   - Resource owner: `BrightForson`
   - Repository access: **Only select repositories** →
     `trading-sandbox-agent`
   - Permissions → Repository permissions → **Actions: Read and write**
     (nothing else — least privilege)
   - Expiration: 90 days (renew quarterly; cron-job.org will email on
     failures when the token dies)
2. **Create cron-job.org account** (free): console.cron-job.org/signup

## Pinger jobs to create (console UI, ~2 min each)

| cron-job.org title | URL | Schedule |
|---|---|---|
| `chat-pinger` | `https://api.github.com/repos/BrightForson/trading-sandbox-agent/actions/workflows/chat.yml/dispatches` | every 15 min |
| `trade-pinger` | `https://api.github.com/repos/BrightForson/trading-sandbox-agent/actions/workflows/trade.yml/dispatches` | every 15 min |

Per-job settings:
- Request method: **POST**
- Headers: `Accept: application/vnd.github+json`,
  `Authorization: Bearer <PAT>`,
  `Content-Type: application/json`
- Body: `{"ref": "main"}`
- Notifications: enable failure alerts (email)

## Verification (agent, automatic)

After both jobs run for ~1h, expect: chat + trade run counts climbing
toward 96/day each, inter-run gaps ~15 min, pain-meter reading `ok`
for both. The pain-meter already measures exactly this — no new
monitoring code needed.

## Rollback

Delete the two cron-job.org jobs. GitHub's native schedules remain in
place (they'll still fire occasionally). Nothing in the repo changes,
so there is no code rollback.

## Follow-ups

- `agent` (hourly) and `scanner` (6h) have not shown the same
  under-delivery — hourly+ cadences are under GitHub's scheduler
  laziness threshold. Leave them on native schedules.
- If the 15-min crons ever need *guaranteed* delivery (real money),
  that's the signal to move to a real host (see STATUS_REPORT.md §16
  venue table — Binance day forces a non-US VPS regardless).
