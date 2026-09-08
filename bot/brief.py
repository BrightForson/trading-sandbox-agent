"""Shared Discord message formatting: money-first, brief, glanceable.

Every tier reports the same way so a phone notification answers one
question instantly: did the money go up or down, and by how much.

Format contract:
  - one short block per tier, emoji-led
  - money line: current equity + change vs the allocation
    (e.g. "💰 $68.00 (was $60 → +$8.00 / +13.3%)")
  - only ACTIVITY gets detail lines (fills, entries, exits). A tier with
    nothing going on says so in one short line and moves on.
"""


def _signed(value, pct=None):
    sign = "+" if value >= 0 else "-"
    txt = f"{sign}${abs(value):,.2f}"
    if pct is not None:
        txt += f" ({sign}{abs(pct):.1f}%)"
    return txt

def money_line(label, equity, baseline):
    """'Tier 3 Bets: $68.00 (was $60 → +$8.00 (+13.3%))'. Pair with emoji_for()."""
    if baseline and baseline > 0:
        delta = equity - baseline
        pct = delta / baseline * 100
        return (f"{label}: ${equity:,.2f} "
                f"(was ${baseline:,.0f} → {_signed(delta, pct)})")
    return f"{label}: ${equity:,.2f}"


def emoji_for(value):
    """Profit/loss emoji for a delta: 📈 up, 📉 down, ➖ flat."""
    if value > 0.005:
        return "📈"
    if value < -0.005:
        return "📉"
    return "➖"
