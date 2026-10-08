#!/usr/bin/env python3
"""Run one Discord chat cycle: read new messages, answer via agent, mark seen."""
from bot.config import config
from bot.binance_data import BinanceDataClient
from bot.chat import run_chat_cycle
from bot.journal import TradeJournal

if __name__ == "__main__":
    market_data = BinanceDataClient(config)
    journal = TradeJournal()
    run_chat_cycle(config, market_data, journal=journal)
