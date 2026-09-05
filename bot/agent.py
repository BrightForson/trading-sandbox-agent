"""AI agent (Tier 2): babysitter + scout, shadow mode.

Babysitter: reviews each open position held by the SMA strategy each cycle —
  price move since entry, SMA alignment, news research — and proposes early
  exits when a thesis breaks. Never overrides the algorithm silently: every
  proposal is validated, logged, and sent to Discord.

Scout: researches the market (headlines, trending, whale proxies, SMA states)
  and proposes its own high-conviction entries the SMA algorithm can't see
  (e.g., news-driven moves before any cross).

Guardrails:
  - shadow mode (config agent.shadow): proposals are logged/alerted, NEVER
    executed. This is the experiment-phase default.
  - deterministic validation of everything the model returns (schema check,
    symbol whitelist, notional/confidence caps)
  - all proposals journaled with simulated outcome tracking
"""
import json
from datetime import datetime

from bot.journal import TradeJournal
from bot.models import ModelManager
from bot.research import research_bundle, headlines_for_symbol
from bot.notify import send_notification


class TradingAgent:
    def __init__(self, cfg, broker, journal=None, model=None):
        self.cfg = cfg
        self.broker = broker
        self.journal = journal or TradeJournal()
        self.model = model or ModelManager(journal=self.journal)
        self.agent_cfg = getattr(cfg, "agent", None) or {}
        self.research_cfg = getattr(cfg, "research", None) or {}
        self.symbols = list(cfg.symbols)
        from bot.shadow import ShadowAccount
        self.shadow = ShadowAccount(cfg, broker, journal=self.journal)

    # ---------------- context building ----------------

    def _price_context(self, symbol):
        """SMA state + recent closes for one symbol, compact for the prompt."""
        try:
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
            tf = TimeFrame(int(self.cfg.timeframe.replace('Min', '')), TimeFrameUnit.Minute)
            df = self.broker.get_crypto_bars(symbol, tf, self.cfg.lookback_bars)
            if df is None or df.empty:
                return None
            df = df.iloc[:-1]  # closed bars only
            close = df['close']
            if len(close) < self.cfg.sma_slow + 1:
                return None
            fast = close.rolling(self.cfg.sma_fast).mean().iloc[-1]
            slow = close.rolling(self.cfg.sma_slow).mean().iloc[-1]
            recent = [float(x) for x in close.tail(24)]
            return {
                "symbol": symbol,
                "last_close": float(close.iloc[-1]),
                "sma_fast": float(fast),
                "sma_slow": float(slow),
                "sma_gap_pct": float((fast - slow) / slow * 100),
                "recent_24_bars": recent,
                "change_24b_pct": (recent[-1] / recent[0] - 1) * 100 if recent[0] else 0.0,
            }
        except Exception as e:
            print(f"[agent] price context failed for {symbol}: {e}")
            return None

    def _position_context(self):
        try:
            positions = list(self.broker.trading_client.get_all_positions())
            out = []
            # Alpaca returns crypto position symbols without the slash (ETHUSD);
            # map back to our ETH/USD format for whitelist checks
            slash_map = {s.replace("/", ""): s for s in self.symbols}
            for p in positions:
                sym = slash_map.get(p.symbol, p.symbol)
                out.append({
                    "symbol": sym,
                    "qty": float(p.qty),
                    "avg_entry": float(p.avg_entry_price),
                    "unrealized_pl": float(p.unrealized_pl),
                    "unrealized_pct": float(p.unrealized_plpc) * 100,
                })
            return out
        except Exception as e:
            print(f"[agent] position context failed: {e}")
            return []

    # ---------------- proposal plumbing ----------------

    def _validate(self, proposal, kind):
        """Deterministic guardrails on anything the model produced."""
        errors = []
        if not isinstance(proposal, dict):
            return None, ["not a dict"]
        action = str(proposal.get("action", "")).upper().strip()
        if action not in ("BUY", "SELL", "HOLD"):
            errors.append(f"bad action {action}")
        symbol = proposal.get("symbol")
        if symbol is not None:
            symbol = self._normalize_symbol(str(symbol))
        if symbol and symbol not in self.symbols:
            errors.append(f"symbol {symbol} not in whitelist")
        try:
            conf = float(proposal.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
            errors.append("confidence not numeric")
        try:
            notional = float(proposal.get("notional", 0))
        except (TypeError, ValueError):
            notional = 0.0
            errors.append("notional not numeric")
        if action == "BUY" and notional > float(self.agent_cfg.get("max_proposed_notional", 50)):
            errors.append(f"notional {notional} above agent cap")
        if errors:
            return None, errors
        return {
            "kind": kind,
            "symbol": symbol,
            "action": action,
            "notional": notional,
            "confidence": conf,
            "rationale": str(proposal.get("rationale", ""))[:500],
        }, []

    def _normalize_symbol(self, raw):
        """Fuzzy-fix model symbol output: 'sol', 'SOL/', 'solusd' -> 'SOL/USD'."""
        s = raw.strip().upper().replace(" ", "")
        if s in self.symbols:
            return s
        base = s.replace("/USD", "").rstrip("/").replace("USD", "")
        for sym in self.symbols:
            if sym.split("/")[0] == base:
                return sym
        return raw

    def _log_and_alert(self, proposal, extra=""):
        ts = datetime.utcnow().isoformat()
        self.journal.log_proposal(
            timestamp=ts,
            source="ai_agent",
            kind=proposal["kind"],
            symbol=proposal.get("symbol"),
            action=proposal["action"],
            notional=proposal.get("notional"),
            confidence=proposal.get("confidence"),
            rationale=proposal.get("rationale"),
        )
        emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(proposal["action"], "⚪")
        mode = "SHADOW (no execution)" if self.agent_cfg.get("shadow", True) else "LIVE"
        # simulate on the virtual shadow account ($20) when in shadow mode
        shadow_note = ""
        if self.agent_cfg.get("shadow", True) and proposal["action"] in ("BUY", "SELL"):
            try:
                if proposal["action"] == "BUY":
                    ok, note = self.shadow.take_buy(
                        proposal.get("symbol"),
                        proposal.get("notional") or self.agent_cfg.get("shadow_max_per_position", 10),
                        rationale=proposal.get("rationale", ""),
                    )
                else:
                    ok, note = self.shadow.take_sell(
                        proposal.get("symbol"),
                        rationale=proposal.get("rationale", ""),
                    )
                if ok:
                    shadow_note = f"\n💵 {note}"
                else:
                    shadow_note = f"\n💵 (shadow acct: {note})"
            except Exception as e:
                print(f"[agent] shadow account execution failed: {e}")
        try:
            send_notification(
                f"{emoji} **Agent proposal — {proposal['kind']}** [{mode}]\n"
                f"**{proposal['action']}** {proposal.get('symbol') or 'market'}"
                + (f" | ${proposal.get('notional'):.0f}" if proposal.get("notional") else "")
                + f" | confidence {proposal.get('confidence', 0):.2f}\n"
                f"Rationale: {proposal.get('rationale', '')}\n{extra}{shadow_note}",
                self.cfg,
            )
        except Exception as e:
            print(f"[agent] proposal alert failed: {e}")

    # ---------------- babysitter ----------------

    def babysit(self):
        """Review open positions; propose early exits if thesis broke."""
        positions = self._position_context()
        if not positions:
            return []
        proposals = []
        for pos in positions:
            price_ctx = self._price_context(pos["symbol"])
            heads = headlines_for_symbol(pos["symbol"], limit=5)
            prompt = f"""You are a risk manager reviewing an open crypto position (paper trading).
Data:
- Position: {json.dumps(pos)}
- Technicals: {json.dumps(price_ctx)}
- Recent news headlines: {json.dumps(heads)}

Task: decide if the original trend thesis is intact. Exit early ONLY on clear
evidence (trend reversal confirmed, breaking news materially negative, thesis
invalidated). Minor dips are noise and should HOLD.

Respond with ONLY a JSON object, max 60 words total:
{{"action": "HOLD"|"SELL", "symbol": "{pos['symbol']}", "confidence": 0.0-1.0, "rationale": "one sentence"}}"""
            try:
                proposal = self.model.generate_json(prompt, max_tokens=600)
            except Exception as e:
                print(f"[agent] babysit LLM call failed for {pos['symbol']}: {e}")
                continue
            valid, errors = self._validate(proposal, "babysitter")
            if valid is None:
                print(f"[agent] babysit proposal rejected for {pos['symbol']}: {errors}")
                continue
            if valid["action"] == "SELL" and valid["confidence"] >= float(self.agent_cfg.get("min_confidence", 0.7)):
                self._log_and_alert(valid)
                proposals.append(valid)
            elif valid["action"] == "SELL":
                # low-confidence exits downgraded to informational
                self._log_and_alert({**valid, "action": "HOLD"},
                                    extra="(low-confidence exit suggestion — logged as HOLD)")
        return proposals

    # ---------------- scout ----------------

    def scout(self):
        """Research the market for high-conviction entries beyond SMA crosses."""
        bundle = research_bundle(self.symbols, self.research_cfg)
        price_ctxs = []
        for sym in self.symbols:
            ctx = self._price_context(sym)
            if ctx:
                price_ctxs.append({k: ctx[k] for k in
                                   ("symbol", "last_close", "sma_gap_pct", "change_24b_pct")})
        # compact headline digests: at most 3 short titles per symbol
        per_sym_heads = {}
        for sym, data in bundle.get("per_symbol", {}).items():
            titles = [i.get("title", "")[:80] for i in (data.get("whales", {}).get("items") or [])[:3]]
            per_sym_heads[sym] = titles
        prompt = f"""You are a crypto trading analyst (paper trading, shadow mode).

DATA (compact):
- 24h moves & SMA gaps: {json.dumps(price_ctxs)}
- Trending coins: {json.dumps(bundle.get('trending', [])[:5])}
- News headlines: {json.dumps(bundle.get('headlines', [])[:6])}
- Whale/flow headlines per symbol: {json.dumps(per_sym_heads)}

TASK: pick AT MOST one trade among {self.symbols} ONLY if evidence is strong
(confluence of technicals + news + sentiment). No strong setup = HOLD.
No rationale text outside the JSON.

OUTPUT: a single JSON object, nothing else, rationale under 40 words:
{{"action": "BUY"|"SELL"|"HOLD", "symbol": one of {self.symbols}, "notional": number <= {self.agent_cfg.get('max_proposed_notional', 50)}, "confidence": 0.0-1.0, "rationale": "1-3 sentences citing the evidence"}}"""
        try:
            proposal = self.model.generate_json(prompt, max_tokens=700)
        except Exception as e:
            print(f"[agent] scout LLM call failed: {e}")
            return []
        valid, errors = self._validate(proposal, "scout")
        if valid is None:
            print(f"[agent] scout proposal rejected: {errors}")
            return []
        if valid["action"] == "HOLD":
            return []
        if valid["confidence"] >= float(self.agent_cfg.get("min_confidence", 0.7)):
            self._log_and_alert(valid)
            return [valid]
        print(f"[agent] scout proposal below confidence threshold: {valid['confidence']}")
        return []

    # ---------------- cycle ----------------

    def run_cycle(self):
        """One agent cycle: health check, babysit open positions, scout for new trades."""
        print(f"[{datetime.now()}] Agent cycle starting (shadow={self.agent_cfg.get('shadow', True)})")
        self.model.daily_health_check()
        proposals = []
        if self.agent_cfg.get("babysitter_enabled", True):
            try:
                proposals += self.babysit()
            except Exception as e:
                print(f"[agent] babysitter error: {e}")
        if self.agent_cfg.get("scout_enabled", True):
            try:
                proposals += self.scout()
            except Exception as e:
                print(f"[agent] scout error: {e}")
        print(f"[{datetime.now()}] Agent cycle done: {len(proposals)} actionable proposals")
        # per-cycle shadow account status to Discord
        try:
            send_notification(f"💵 {self.shadow.status_line()}", self.cfg)
        except Exception as e:
            print(f"[agent] shadow status alert failed: {e}")
        return proposals
