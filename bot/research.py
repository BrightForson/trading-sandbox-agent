"""Research tools for the AI agent — all free tiers, no paid APIs.

  - RSS headlines (Cointelegraph + CoinDesk) — replaces CryptoPanic
  - CoinGecko keyless market stats + trending
  - Tavily general web search, guarded by hard daily/monthly budget counters
    persisted in journal meta (never exceed the free 1500 credits/month)

Every function degrades gracefully: on failure it returns empty/neutral data
so the agent can still reason with what it has.
"""
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from bot.journal import TradeJournal

load_dotenv()

HEADERS = {"User-Agent": "Mozilla/5.0 (trading-sandbox-agent research)"}
TAVILY_URL = "https://api.tavily.com/search"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]

SYMBOL_TO_NAME = {
    "BTC/USD": "bitcoin", "ETH/USD": "ethereum", "SOL/USD": "solana",
    "bitcoin": "bitcoin", "ethereum": "ethereum", "solana": "solana",
}

_journal = None


def _get_journal():
    global _journal
    if _journal is None:
        _journal = TradeJournal()
    return _journal


# ---------------- RSS ----------------

def _month_key():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _day_key():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fetch_rss_headlines(limit=10, feeds=None):
    """Recent crypto news headlines from RSS. Returns list[str]."""
    out = []
    for url in feeds or RSS_FEEDS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            for item in root.iter("item"):
                title = item.findtext("title")
                if title:
                    out.append(title.strip())
                if len(out) >= limit:
                    break
        except Exception as e:
            print(f"[research] RSS fetch failed for {url}: {e}")
    return out[:limit]


def headlines_for_symbol(symbol, limit=5, all_headlines=None):
    """Filter general headlines down to ones mentioning the coin's name/ticker."""
    name = SYMBOL_TO_NAME.get(symbol, "").lower()
    if not name:
        return []
    if all_headlines is None:
        all_headlines = fetch_rss_headlines(limit=30)
    ticker = symbol.split("/")[0].lower()
    out = []
    for h in all_headlines:
        low = h.lower()
        if name in low or ticker in low:
            out.append(h)
        if len(out) >= limit:
            break
    return out


# ---------------- CoinGecko ----------------

def market_stats(symbols=None):
    """Simple price + 24h change per symbol (keyless CoinGecko API)."""
    ids = [SYMBOL_TO_NAME.get(s) for s in (symbols or [])]
    ids = [i for i in ids if i]
    if not ids:
        return {}
    try:
        resp = requests.get(
            COINGECKO_PRICE_URL,
            params={"ids": ",".join(ids), "vs_currencies": "usd", "include_24hr_change": "true"},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        out = {}
        for sym in (symbols or []):
            coin = data.get(SYMBOL_TO_NAME.get(sym, ""), {})
            if coin:
                out[sym] = {
                    "price": coin.get("usd"),
                    "change_24h_pct": coin.get("usd_24h_change"),
                }
        return out
    except Exception as e:
        print(f"[research] CoinGecko price failed: {e}")
        return {}


def trending_coins():
    """Top-7 trending coins on CoinGecko (whale/retail interest proxy)."""
    try:
        resp = requests.get(COINGECKO_TRENDING_URL, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return [item["item"]["name"] for item in data.get("coins", [])[:7]]
    except Exception as e:
        print(f"[research] CoinGecko trending failed: {e}")
        return []


# ---------------- Tavily (budget-guarded) ----------------

def _tavily_counters(journal):
    day, month = _day_key(), _month_key()
    day_count = journal.get_meta(f"tavily_count_{day}") or "0"
    month_count = journal.get_meta(f"tavily_count_{month}") or "0"
    return int(day_count), int(month_count), day, month


def _tavily_increment(journal, day, month):
    journal.set_meta(f"tavily_count_{day}", str(int(journal.get_meta(f"tavily_count_{day}") or "0") + 1))
    journal.set_meta(f"tavily_count_{month}", str(int(journal.get_meta(f"tavily_count_{month}") or "0") + 1))


def tavily_search(query, cfg=None, max_results=5):
    """
    General web search via Tavily. Hard budget guardrails:
      - daily limit (config research.tavily_daily_limit, default 30)
      - monthly limit (config research.tavily_monthly_limit, default 1000)
    Returns [] when out of budget or on any failure (graceful degradation).
    """
    journal = _get_journal()
    api_key = os_getenv_tavily()
    if not api_key:
        return []
    day_count, month_count, day, month = _tavily_counters(journal)
    daily_limit = int((cfg or {}).get("tavily_daily_limit", 30)) if cfg else 30
    monthly_limit = int((cfg or {}).get("tavily_monthly_limit", 1000)) if cfg else 1000
    if day_count >= daily_limit or month_count >= monthly_limit:
        print(f"[research] Tavily budget guard: {day_count}/{daily_limit} today, "
              f"{month_count}/{monthly_limit} this month — skipping search")
        return []
    try:
        resp = requests.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": query, "max_results": max_results, "search_depth": "basic"},
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"[research] Tavily returned {resp.status_code}")
            return []
        _tavily_increment(journal, day, month)
        payload = resp.json()
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""), "content": (r.get("content") or "")[:400]}
            for r in payload.get("results", [])
        ]
    except Exception as e:
        print(f"[research] Tavily search failed: {e}")
        return []


def os_getenv_tavily():
    return os.getenv("TAVILY_API_KEY")


# ---------------- whale activity proxy ----------------

def whale_activity(symbol, cfg=None):
    """
    Whale/insider activity approximation. Tavily is expensive (budget) so it's
    used at most ONCE per symbol per day (cached in meta); otherwise RSS
    headlines filtered for the symbol (free).
    """
    journal = _get_journal()
    day = _day_key()
    cache_key = f"whale_cache_{day}_{symbol.replace('/', '_')}"
    if journal.get_meta(cache_key):
        cached = journal.get_meta(f"whale_cache_data_{symbol.replace('/', '_')}")
        if cached:
            import json as _json
            try:
                return _json.loads(cached)
            except Exception:
                pass
    name = SYMBOL_TO_NAME.get(symbol, symbol)
    results = tavily_search(f"{name} whale accumulation large transfers news this week", cfg, max_results=5)
    if results:
        import json as _json
        payload = {"source": "tavily", "items": results}
        journal.set_meta(cache_key, "done")
        journal.set_meta(f"whale_cache_data_{symbol.replace('/', '_')}", _json.dumps(payload))
        return payload
    heads = headlines_for_symbol(symbol, limit=5)
    if heads:
        return {"source": "rss", "items": [{"title": h, "url": "", "content": ""} for h in heads]}
    return {"source": "none", "items": []}


# ---------------- convenience bundle ----------------

def research_bundle(symbols=None, cfg=None):
    """Everything the agent needs in one call: headlines, stats, trending, whales."""
    symbols = symbols or ["BTC/USD", "ETH/USD", "SOL/USD"]
    all_heads = fetch_rss_headlines(limit=20)
    bundle = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_stats": market_stats(symbols),
        "trending": trending_coins(),
        "headlines": all_heads,
        "per_symbol": {},
    }
    for sym in symbols:
        bundle["per_symbol"][sym] = {
            "headlines": headlines_for_symbol(sym, limit=5, all_headlines=all_heads),
            "whales": whale_activity(sym, cfg),
        }
    return bundle
