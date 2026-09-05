#!/usr/bin/env python3
"""Run one Discord chat cycle: read new messages, answer via agent, mark seen."""
from bot.config import config
from bot.broker import AlpacaBroker
from bot.chat import run_chat_cycle
from bot.journal import TradeJournal

if __name__ == "__main__":
    broker = AlpacaBroker(config.alpaca_api_key_id, config.alpaca_api_secret_key)
    journal = TradeJournal()
    run_chat_cycle(config, broker, journal=journal)
