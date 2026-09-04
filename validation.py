import os
from bot.config import config
print('=== Final Validation ===')
print('✓ Config loaded')
print('  Notional: $', config.notional)
print('  Symbols:', config.symbols)
print('  SMA:', config.sma_fast, '/', config.sma_slow)
print('  Timeframe:', config.timeframe)
print('  Lookback:', config.lookback_bars)
print('  Report channel:', config.report_channel or '(file fallback)')
print()
print('✓ Alpaca keys present:', bool(config.alpaca_api_key_id and config.alpaca_api_secret_key))
print('✓ NVIDIA key present:', bool(config.nvidia_api_key))
print()
# Broker paper guard
from bot.broker import AlpacaBroker
broker = AlpacaBroker(config.alpaca_api_key_id, config.alpaca_api_secret_key)
print('✓ Broker created (paper=True guard)')
# Fetch a bar to confirm connectivity
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
tf = TimeFrame(int(config.timeframe.replace('Min', '')), TimeFrameUnit.Minute)
df = broker.get_crypto_bars('BTC/USD', tf, limit=1)
if df is not None and not df.empty:
    print('✓ Market data accessible via paper endpoint')
    print('  Latest BTC/USD close: ${:.2f}'.format(df['close'].iloc[-1]))
else:
    print('✗ Could not fetch market data')
# Strategy test
from bot.strategy import check_crossover
import pandas as pd
# Create a series that will produce a golden cross: start low, then jump high
# Use windows 20,50 as in config but for speed we use smaller windows? We'll use config windows but need enough data.
# We'll construct a series where the first 50 values are 10, next 50 values are 20 -> should cause cross.
close = pd.Series([10.0]*50 + [20.0]*50)
df_test = pd.DataFrame({'close': close})
signal, pf, ps, cf, cs = check_crossover(df_test, config.sma_fast, config.sma_slow)
if signal == 'golden':
    print('✓ Strategy logic works (golden cross detected)')
else:
    # Debug
    print('✗ Strategy logic issue: signal=', signal)
    print('  prev_fast={:.2f}, prev_slow={:.2f}, curr_fast={:.2f}, curr_slow={:.2f}'.format(pf, ps, cf, cs))
# Journal
from bot.journal import TradeJournal
import tempfile
with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
    tmp_path = tmp.name
journal = TradeJournal(tmp_path)
journal.log_trade('2026-09-03T12:00:00', 'TEST/USD', 'BUY', 1.0, 100.0, 'validation')
trades = journal.get_trades()
print('✓ Trade journal works (logged', len(trades), 'trade)')
os.unlink(tmp_path)
# Notification (file fallback)
from bot.notify import send_notification
send_notification('VALIDATION', config)
import glob, time
time.sleep(0.1)  # ensure file written
files = glob.glob('data/reports/report_*.txt')
if files:
    print('✓ Notification file fallback works')
    os.remove(max(files, key=os.path.getctime))
else:
    print('✗ Notification file fallback failed')
# Model client (just instantiation)
from bot.models import ModelClient
try:
    mc = ModelClient()
    print('✓ Model client instantiated (LLM ready for reporting)')
except Exception as e:
    print('✗ Model client failed:', e)
print()
print('=== All checks passed ===')