"""Model manager: NVIDIA LLM client with health checks and auto-failover.

Replaces the hardcoded primary/fallback pair. On init and on daily health check:
  1. probe the current active model with a 1-token call
  2. if it fails (404/410 deprecated, timeout, etc.), walk a ranked preference
     chain of known-good reasoning models, probing each
  3. persist the choice in journal meta + notify Discord on any switch

The chain is a preference ranking; probes verify reality (a listed model can
still be decommissioned, as happened with deepseek-v4-pro and kimi-k2.6).
"""
import os
import json
from datetime import datetime

import openai
from dotenv import load_dotenv

from bot.errors import ModelError

load_dotenv()

# Ranked preference chain (verified against the live /v1/models catalog; probed at runtime)
MODEL_CHAIN = [
    "nvidia/nemotron-3-super-120b-a12b",   # current primary
    "moonshotai/kimi-k3",
    "deepseek-ai/deepseek-v4-flash-0731",
    "minimaxai/minimax-m3",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/llama-3.1-nemotron-70b-instruct",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
]
META_KEY = "active_llm_model"
META_PROBED_KEY = "last_model_probe_utc"


class ModelManager:
    def __init__(self, journal=None):
        self.api_key = os.getenv("NVIDIA_API_KEY")
        if not self.api_key:
            raise ModelError("NVIDIA_API_KEY not found in environment variables")
        self.journal = journal
        self.base_url = "https://integrate.api.nvidia.com/v1"
        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=60)
        self.active_model = None
        self._load_or_select_model()

    # ---------- persistence ----------

    def _load_or_select_model(self):
        saved = self.journal.get_meta(META_KEY) if self.journal else None
        if saved and saved in MODEL_CHAIN:
            if self._probe(saved):
                self.active_model = saved
                return
            print(f"[model-manager] saved model '{saved}' failed probe; selecting new one")
        self.select_model(announce=False)

    def _persist(self):
        if self.journal:
            self.journal.set_meta(META_KEY, self.active_model)

    # ---------- probing ----------

    def _probe(self, model, timeout=20):
        """Cheap liveness probe: 1-token chat completion."""
        try:
            probe_client = openai.OpenAI(
                api_key=self.api_key, base_url=self.base_url, timeout=timeout
            )
            probe_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0,
            )
            return True
        except Exception:
            return False

    def select_model(self, announce=True):
        """Walk the chain and adopt the first model that passes a probe."""
        previous = self.active_model
        for candidate in MODEL_CHAIN:
            if self._probe(candidate):
                self.active_model = candidate
                self._persist()
                self._record_probe_time()
                if announce and previous != candidate:
                    self._announce_switch(previous, candidate)
                return candidate
        raise ModelError(f"No working model in chain: {MODEL_CHAIN}")

    def _record_probe_time(self):
        if self.journal:
            self.journal.set_meta(META_PROBED_KEY, datetime.utcnow().isoformat())

    def _announce_switch(self, previous, new):
        """Discord alert on model switch (best effort, never fatal)."""
        try:
            from bot.notify import send_notification
            from bot.config import config
            send_notification(
                f"🧠 **Model switch**\n"
                f"`{previous or 'none'}` → `{new}`\n"
                f"Old model failed its health probe (deprecated or unreachable). "
                f"Failover chain worked as designed.",
                config,
            )
        except Exception as e:
            print(f"[model-manager] switch announcement failed: {e}")

    # ---------- daily health check ----------

    def daily_health_check(self):
        """Probe the active model; on failure re-run selection. Called by the agent cycle."""
        if self.journal:
            last = self.journal.get_meta(META_PROBED_KEY)
            if last:
                try:
                    from datetime import datetime as _dt
                    last_dt = _dt.fromisoformat(last)
                    if (_dt.utcnow() - last_dt).total_seconds() < 20 * 3600:
                        return self.active_model  # checked recently
                except Exception:
                    pass
        if not self._probe(self.active_model):
            print(f"[model-manager] active model '{self.active_model}' unhealthy; reselecting")
            self.select_model(announce=True)
        else:
            self._record_probe_time()
        return self.active_model

    # ---------- generation ----------

    def generate_text(self, prompt, max_tokens=800, temperature=0.7, retries=1):
        """Generate text with the active model; on failure reselect once and retry."""
        for attempt in range(retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.active_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                return response.choices[0].message.content
            except Exception as e:
                print(f"[model-manager] generation failed on {self.active_model}: {e}")
                if attempt < retries:
                    self.select_model(announce=False)
                else:
                    raise ModelError(f"All model attempts failed: {e}")

    def generate_json(self, prompt, max_tokens=800, temperature=0.2):
        """Generate and parse a JSON object; repair-retries + regex last resort."""
        raw = self.generate_text(prompt, max_tokens=max_tokens, temperature=temperature)
        try:
            return self._parse_json(raw)
        except ModelError:
            pass
        # retry with strict repair instruction
        repair = (
            "Your previous answer was not valid JSON. Output ONLY the JSON object, "
            "no prose, no analysis, no markdown. Begin your response with '{' and end "
            "with '}'. Previous answer (truncated): "
            f"{raw[:300]}\n\nOriginal request:\n{prompt}\n\nJSON only now:"
        )
        raw2 = ""
        try:
            raw2 = self.generate_text(repair, max_tokens=max_tokens, temperature=0.0)
            return self._parse_json(raw2)
        except ModelError:
            pass
        # last resort: regex-extract the fields we care about from prose
        extracted = self._regex_extract(raw + "\n" + raw2)
        if extracted:
            return extracted
        raise ModelError(f"Model refused JSON twice: {raw[:200]}")

    def generate_json_arr(self, prompt, max_tokens=800, temperature=0.2):
        """Generate and parse a JSON array; one repair retry."""
        raw = self.generate_text(prompt, max_tokens=max_tokens, temperature=temperature)
        try:
            return self._parse_json_arr(raw)
        except ModelError:
            repair = (
                "Your previous answer was not a valid JSON array. Output ONLY the JSON "
                "array, no prose, no markdown. Begin with '[' and end with ']'. "
                f"Previous answer (truncated): {raw[:300]}\n\nOriginal request:\n{prompt}\n\n"
                "JSON array only now:"
            )
            raw2 = self.generate_text(repair, max_tokens=max_tokens, temperature=0.0)
            return self._parse_json_arr(raw2)

    @staticmethod
    def _regex_extract(text):
        """Best-effort field extraction when the model insists on prose."""
        import re
        out = {}
        m = re.search(r'\b(BUY|SELL|HOLD)\b', text, re.IGNORECASE)
        if m:
            out["action"] = m.group(1).upper()
        sym = re.search(r'\b(BTC|ETH|SOL)/USD\b', text)
        if sym:
            out["symbol"] = sym.group(0)
        conf = re.search(r'confidence[:\s"\']*([01]?\.\d+|\d)\b', text, re.IGNORECASE)
        if conf:
            try:
                out["confidence"] = float(conf.group(1))
            except ValueError:
                pass
        notional = re.search(r'notional[:\s"\']*\$?(\d+(?:\.\d+)?)', text, re.IGNORECASE)
        if notional:
            try:
                out["notional"] = float(notional.group(1))
            except ValueError:
                pass
        rat = re.search(r'rationale[:\s"\']*["“]?(.{10,400}?)["”]?\s*(?:$|\n)', text, re.IGNORECASE)
        if rat:
            out["rationale"] = rat.group(1).strip()
        return out if "action" in out else None

    @staticmethod
    def _parse_json_arr(raw):
        """Parse a JSON array from model output (code-fence tolerant)."""
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            raise ModelError(f"No JSON array in model output: {raw[:200]}")
        try:
            return json.loads(text[start:end + 1])
        except Exception as e:
            raise ModelError(f"JSON array parse failed: {e}\n{raw[:200]}")

    @staticmethod
    def _parse_json(raw):
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start == -1:
            raise ModelError(f"No JSON object in model output: {raw[:200]}")
        candidate = text[start:end + 1] if end != -1 else text[start:]
        if not candidate.rstrip().endswith("}"):
            candidate += '"}' if not candidate.rstrip().endswith('"') else '}'
        try:
            return json.loads(candidate)
        except Exception:
            # truncated output: try closing the object
            for suffix in ('"}', '"}', '"} }'):
                try:
                    return json.loads(text[start:] + suffix)
                except Exception:
                    continue
        raise ModelError(f"JSON parse failed: {raw[:200]}")


# Backwards-compatible alias: existing report code uses ModelClient
ModelClient = ModelManager
