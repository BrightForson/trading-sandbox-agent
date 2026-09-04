import pandas as pd

def compute_sma(series, window):
    """Compute simple moving average."""
    return series.rolling(window=window).mean()

def check_crossover(df, fast_window, slow_window):
    """
    Check for golden or death cross.
    :param df: DataFrame with 'close' column
    :param fast_window: SMA fast period
    :param slow_window: SMA slow period
    :return: tuple (signal, prev_fast, prev_slow, curr_fast, curr_slow)
             signal: "golden", "death", or None
    """
    # Ensure we have enough data
    if len(df) < slow_window:
        return None, None, None, None, None
    
    close = df['close']
    fast = compute_sma(close, fast_window)
    slow = compute_sma(close, slow_window)
    
    # Get last two values
    prev_fast = fast.iloc[-2]
    prev_slow = slow.iloc[-2]
    curr_fast = fast.iloc[-1]
    curr_slow = slow.iloc[-1]
    
    # Check for crossovers
    if prev_fast <= prev_slow and curr_fast > curr_slow:
        return "golden", prev_fast, prev_slow, curr_fast, curr_slow
    elif prev_fast >= prev_slow and curr_fast < curr_slow:
        return "death", prev_fast, prev_slow, curr_fast, curr_slow
    else:
        return None, prev_fast, prev_slow, curr_fast, curr_slow