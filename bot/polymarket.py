"""Polymarket scanner (Tier 3): read-only API, paper bets only.

Scans the public Gamma API (no account, no auth, free) for:
  1. near-resolution favorites: a watchlist only, never an automatic bet
  2. stale-odds candidates: low-volume markets whose odds look out of line
     with the LLM's own probability estimate (LLM flags mispricings)
  3. high-volume momentum: heavy one-sided volume as a signal

Everything is PAPER: bets are logged to the journal's `bets` table with
simulated stakes; nothing is ever placed on a real prediction market.
"""
import json
from datetime import datetime, timezone

import requests

from bot.journal import TradeJournal
from bot.models import ModelManager
from bot.notify import send_notification

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
HEADERS = {"User-Agent": "Mozilla/5.0 (trading-sandbox-agent scanner)"}


def _cfg_val(cfg, key, default):
    return float((getattr(cfg, "scanner", None) or {}).get(key, default))


def _fetch_markets(limit=100, active_only=True, closed=False):
    params = {
        "limit": limit,
        "active": str(active_only).lower(),
        "closed": str(closed).lower(),
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        resp = requests.get(GAMMA_MARKETS_URL, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[scanner] Gamma fetch failed: {e}")
        return []


def _parse_market(m):
    """Normalize a Gamma market into a compact dict; None if unusable."""
    try:
        outcomes = json.loads(m.get("outcomes", "[]"))
        prices = json.loads(m.get("outcomePrices", "[]"))
        if len(prices) != 2 or len(outcomes) != 2:
            return None
        end = m.get("endDate") or ""
        end_ts = None
        if end:
            try:
                end_ts = datetime.fromisoformat(end.replace("Z", "+00:00"))
            except Exception:
                end_ts = None
        return {
            "id": m.get("id"),
            "question": (m.get("question") or "")[:200],
            "slug": m.get("slug", ""),
            "outcomes": outcomes,
            "yes_price": float(prices[0]),
            "no_price": float(prices[1]),
            "volume_24h": float(m.get("volume24hr") or 0),
            "volume_total": float(m.get("volume") or 0),
            "liquidity": float(m.get("liquidityNum") or m.get("liquidity") or 0),
            "end_ts": end_ts,
            "active": bool(m.get("active", False)),
            "closed": bool(m.get("closed", False)),
        }
    except Exception:
        return None


def scan_near_resolution(cfg, markets=None):
    """Return expensive favorites as a research watchlist, never as an edge."""
    max_days = _cfg_val(cfg, "near_resolution_days", 3)
    min_price = _cfg_val(cfg, "near_resolution_min_price", 0.97)
    min_vol = _cfg_val(cfg, "min_market_volume", 50000)
    watchlist_only = bool((getattr(cfg, "scanner", None) or {}).get("near_resolution_watchlist_only", True))
    markets = markets if markets is not None else _fetch_markets(limit=100)
    now = datetime.now(timezone.utc)
    out = []
    for m in markets:
        parsed = _parse_market(m)
        if parsed is None or not parsed["end_ts"]:
            continue
        days_left = (parsed["end_ts"] - now).total_seconds() / 86400
        if days_left < 0 or days_left > max_days:
            continue
        if parsed["volume_24h"] < min_vol and parsed["volume_total"] < min_vol:
            continue
        if parsed["yes_price"] >= min_price:
            side, price = parsed["outcomes"][0], parsed["yes_price"]
        elif parsed["no_price"] >= min_price:
            side, price = parsed["outcomes"][1], parsed["no_price"]
        else:
            continue
        out.append({**parsed, "strategy": "near_resolution", "side": side, "price": price,
                    "days_left": round(days_left, 2),
                    "paper_bet_allowed": not watchlist_only and days_left > 0.5})
    return out


def _expected_value(stake, price, probability, friction_pct):
    """Return fee/slippage-adjusted expected value for a fixed cash stake."""
    if stake <= 0 or not 0 < price < 1 or not 0 <= probability <= 1:
        return None
    estimated_cost = stake * max(0.0, friction_pct) / 100.0
    total_cost = stake + estimated_cost
    expected_payout = probability * (stake / price)
    expected_value = expected_payout - total_cost
    return {
        "fee": estimated_cost,
        "total_cost": total_cost,
        "expected_value": expected_value,
        "expected_value_pct": expected_value / total_cost * 100,
    }


def scan_llm_mispricing(cfg, markets=None, model=None, journal=None):
    """Strategy 2: LLM estimates true probability on selected markets; flag gaps."""
    threshold = _cfg_val(cfg, "mispricing_threshold", 0.08)
    max_llm = int(_cfg_val(cfg, "max_llm_markets", 5))
    markets = markets if markets is not None else _fetch_markets(limit=100)
    journal = journal or TradeJournal()
    if model is None:
        model = ModelManager(journal=journal)
    # pick liquid, non-trivial markets for estimation
    candidates = []
    for m in markets:
        parsed = _parse_market(m)
        if parsed is None:
            continue
        if parsed["volume_24h"] < _cfg_val(cfg, "min_market_volume", 50000) / 10:
            continue
        if parsed["end_ts"] is None:
            continue
        if 0.15 <= parsed["yes_price"] <= 0.85:
            candidates.append(parsed)
        if len(candidates) >= max_llm:
            break
    if not candidates:
        return []
    lines = [f"- {c['question']} (current YES price: {c['yes_price']:.2f})" for c in candidates]
    prompt = f"""You are a prediction-market analyst. For each market, estimate the
true probability (0.00-1.00) of the FIRST outcome (YES) resolving true, using
your world knowledge. Be calibrated and skeptical of hype.

Markets:
{chr(10).join(lines)}

Respond with ONLY a JSON array of objects:
[{{"question": "...", "true_prob": 0.0-1.0}}, ...]"""
    try:
        # reasoning models need headroom to think before emitting the array
        est = model.generate_json_arr(prompt, max_tokens=2000)
    except Exception as e:
        print(f"[scanner] LLM mispricing call failed: {e}")
        return []
    out = []
    for item in est:
        q = item.get("question", "")
        match = next((c for c in candidates if c["question"] == q), None)
        if match is None:
            continue
        try:
            tp = float(item.get("true_prob"))
        except (TypeError, ValueError):
            continue
        if not 0.0 <= tp <= 1.0:
            continue
        gap = tp - match["yes_price"]
        if abs(gap) >= threshold:
            price = match["yes_price"] if gap > 0 else match["no_price"]
            probability = tp if gap > 0 else 1 - tp
            ev = _expected_value(
                _cfg_val(cfg, "stake", 20), price, probability,
                _cfg_val(cfg, "friction_pct", 1.0),
            )
            if ev and ev["expected_value_pct"] >= _cfg_val(cfg, "min_expected_value_pct", 2.0):
                out.append({**match, "strategy": "llm_mispricing", "llm_prob": round(tp, 3),
                            "estimated_probability": round(probability, 3), "gap": round(gap, 3),
                            "side": match["outcomes"][0] if gap > 0 else match["outcomes"][1],
                            "price": price, "paper_bet_allowed": True, **ev})
    return out


def scan(cfg, journal=None, model=None):
    """Full scanner cycle: both strategies, paper-log and alert the best finds."""
    from bot.wallet import BettingWallet
    journal = journal or TradeJournal()
    print(f"[{datetime.now()}] Scanner cycle starting (paper bets only)")
    wallet = BettingWallet(cfg, journal=journal)
    wallet_bust = wallet.is_bust()
    raw_markets = _fetch_markets(limit=100)

    watchlist = scan_near_resolution(cfg, markets=raw_markets)
    finds = []
    try:
        finds += scan_llm_mispricing(cfg, markets=raw_markets, model=model, journal=journal)
    except Exception as e:
        print(f"[scanner] mispricing strategy failed: {e}")

    stake = _cfg_val(cfg, "stake", 20)
    max_exposure = _cfg_val(cfg, "max_total_open_exposure", 100)
    paper_finds = [f for f in finds if f.get("paper_bet_allowed")]
    if wallet_bust:
        print("[scanner] wallet BUST — scanning watchlist only, no new paper bets")
        send_notification(
            f"💰 {wallet.status_line()} — no new paper bets until epoch reset",
            cfg,
        )
        paper_finds = []
    for f in paper_finds[:5]:
        try:
            market = f["slug"] or f["question"][:60]
            if journal.has_open_bet(market, f["side"]):
                continue
            fee = float(f.get("fee", 0.0))
            if journal.open_bet_exposure() + stake + fee > max_exposure:
                print("[scanner] open paper-bet exposure cap reached")
                break
            journal.log_bet(
                timestamp=datetime.now(timezone.utc).isoformat(),
                market=market,
                question=f["question"],
                side=f["side"],
                price=f["price"],
                stake=stake,
                outcome="open",
                notes=(f"strategy={f['strategy']} llm_prob={f.get('llm_prob')} "
                       f"gap={f.get('gap')} ev_pct={f.get('expected_value_pct'):.2f}"),
                fee=fee,
                estimated_probability=f.get("estimated_probability"),
                expected_value=f.get("expected_value"),
            )
            send_notification(
                f"🎯 **Paper bet — {f['strategy']}** (Polymarket scan)\n"
                f"**{f['question'][:120]}**\n"
                f"Side: **{f['side']}** @ {f['price']:.3f} | Stake ${stake:.0f} (paper)\n"
                f"Estimated probability {f['estimated_probability']:.3f} | "
                f"expected value ${f['expected_value']:+.2f} after ${fee:.2f} estimated friction\n"
                f"24h vol ${f['volume_24h']:,.0f} | ends {f.get('end_ts')}",
                cfg,
            )
        except Exception as e:
            print(f"[scanner] failed to log/alert a find: {e}")

    if watchlist:
        print(f"[scanner] {len(watchlist)} near-resolution favorites kept as watchlist only")
    print(f"[{datetime.now()}] Scanner done: {len(paper_finds)} EV-qualified finds, watchlist={len(watchlist)}")
    return {"paper_finds": paper_finds, "watchlist": watchlist, "bust": wallet_bust}


def settle_open_bets(cfg, journal=None):
    """Check open paper bets against resolved markets (best effort)."""
    journal = journal or TradeJournal()
    open_bets = journal.get_open_bets()
    if not open_bets:
        return []
    settled = []
    slugs = {b[2] for b in open_bets}
    for slug in slugs:
        try:
            resp = requests.get(GAMMA_MARKETS_URL, params={"slug": slug}, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                continue
            m = data[0]
            if not m.get("closed"):
                continue
            resolved = json.loads(m.get("outcomePrices", "[]"))
            winner_idx = 0 if float(resolved[0]) >= 0.999 else 1
            winner = json.loads(m.get("outcomes", "[]"))[winner_idx]
            for b in open_bets:
                if b[2] != slug:
                    continue
                won = (b[4] == winner)
                payout = b[6] / b[5] if won else 0.0
                journal.update_bet(b[0], "won" if won else "lost", round(payout, 2))
                settled.append((b, won, payout))
                try:
                    send_notification(
                        f"{'🏆' if won else '💀'} **Paper bet settled**\n"
                        f"{b[3]}\n{'Won' if won else 'Lost'} {b[4]} @ {b[5]:.3f} — "
                        f"payout ${payout:.2f} on ${b[6]:.0f} stake",
                        cfg,
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"[scanner] settle check failed for {slug}: {e}")
    return settled
