# Sandbox Paper Trading Bot – Project Summary

## Overview
A Python 3.12 application that trades cryptocurrency (BTC/USD, ETH/USD, SOL/USD) on Alpaca’s paper trading platform using a deterministic SMA‑crossover strategy (20‑period / 50‑period). Trade decisions involve **no LLMs**; LLMs are used only for generating a short narrative in the daily report.

## File Structure
```
trading-sandbox-agent/
├── .env / .env.example
├── config.yaml
├── requirements.txt
├── run_bot.py
├── run_report.py
├── bot/
│   ├── __init__.py
│   ├── config.py          # Loads config & environment variables
│   ├── broker.py          # Alpaca paper‑trading client (paper=True guard)
│   ├── strategy.py        # Deterministic SMA crossover logic
│   ├── trader.py          # Main loop (15‑min polling, per‑symbol error isolation)
│   ├── models.py          # LLM client for reporting only (NVIDIA primary, DeepSeek fallback)
│   ├── journal.py         # SQLite trade journal
│   ├── report.py          # P&L/win‑rate calculation + LLM narrative generation
│   ├── notify.py          # Notification via openclaw or file fallback
│   └── errors.py          # Custom exception classes
└── data/
    ├── trades.db          # SQLite trade log (created on first run)
    └── reports/           # Directory for text reports (gitignored)
```

## Configuration
### config.yaml
```yaml
symbols:
  - BTC/USD
  - ETH/USD
  - SOL/USD
sma_fast: 20
sma_slow: 50
notional: 100               # $ per trade (adjusted from 10000 for $100 paper balance)
report_channel: null        # Discord channel ID; null → file fallback
timeframe: 15Min
lookback_bars: 120
```
### .env (replace placeholders with real keys)
```env
ALPACA_API_KEY_ID=your_alpaca_paper_key_id
ALPACA_API_SECRET_KEY=your_alpaca_paper_secret_key
NVIDIA_API_KEY=your_nvidia_api_key_from_build_nvidia
# OPENROUTER_API_KEY is unused by the bot
```

## How It Works
### Trader (`run_bot.py`)
- Runs immediately, then every 15 minutes.
- For each symbol:
  - Fetches the last 120 (15‑minute) bars from Alpaca (paper endpoint).
  - Computes SMA20 and SMA50 on closing prices.
  - Detects crossovers:
    - **Golden cross**: prev SMA20 ≤ SMA50 and now SMA20 > SMA50 → BUY (if flat)
    - **Death cross**: prev SMA20 ≥ SMA50 and now SMA20 < SMA50 → SELL (if holding)
  - Quantity = `notional / latest close price`.
  - Places a market order via Alpaca paper‑trading API.
  - Logs the trade to SQLite (`data/trades.db`) with:
    `timestamp, symbol, action, qty, price, reasoning`
    (e.g., `"golden cross: SMA20 182.30 crossed above SMA50 181.95"`).

### Reporter (`run_report.py`)
- Reads all trades from `data/trades.db`.
- Computes total P&L, win‑rate, number of trades, etc., **using pure Python** (no LLM).
- Builds a prompt that includes these statistics.
- Calls the LLM (NVIDIA API with DeepSeek fallback) to produce a short narrative summary.
- Outputs a full report (stats + narrative) and:
  - Tries to send via Discord using `openclaw message send` (if `report_channel` set).
  - Falls back to writing a timestamped text file in `data/reports/` if Discord is not configured or fails.

## Safety & Error Handling
- **Paper‑trading guarantee**: `TradingClient(..., paper=True)` hard‑codes the endpoint to `https://paper-api.alpaca.markets`. Live trading is impossible.
- **Per‑symbol isolation**: Errors in one symbol’s data fetch, order placement, etc., are caught and logged; the bot continues with the next symbol.
- **Notification fallback**: If Discord is not configured or `openclaw` fails, the report is written to `data/reports/`.
- **LLM usage restricted**: The LLM is invoked **only** for the narrative section of the daily report; all trading logic and P&L calculations are deterministic and LLM‑free.

## Setup & Execution
1. **Create & activate a virtual environment**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
3. **Configure `.env`** with your actual Alpaca paper API keys and NVIDIA API key.
4. **Ensure your Alpaca paper account has at least the notional amount** (e.g., $100) available buying power.
5. **Start the bot**
   ```bash
   python run_bot.py   # runs continuously; press Ctrl+C to stop
   ```
6. **Generate a manual report** (after some trading activity)
   ```bash
   python run_report.py
   ```

## Next Steps for Your Claude Project
- Upload the entire `trading-sandbox-agent` folder as the codebase for your Claude project.
- The bot is ready to run in paper mode; you can observe its behavior, inspect `data/trades.db`, and view generated reports in `data/reports/`.
- Feel free to extend or modify the bot (e.g., add more symbols, adjust SMA periods, enhance reporting) by editing the relevant files in the `bot/` directory.

---

**Your sandbox paper trading bot is now fully built, validated, and ready to run.**