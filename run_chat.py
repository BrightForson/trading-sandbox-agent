#!/usr/bin/env python3
"""Run one Discord chat cycle: read new messages, answer via agent, mark seen."""
from bot.config import config
from bot.broker import make_broker
from bot.chat import run_chat_cycle
from bot.journal import TradeJournal

if __name__ == "__main__":
    broker = make_broker(config)
    journal = TradeJournal()
    run_chat_cycle(config, broker, journal=journal)
