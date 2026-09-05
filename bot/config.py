import os
import yaml
from dotenv import load_dotenv

load_dotenv()  # take environment variables from .env.

class Config:
    def __init__(self, config_path="config.yaml"):
        with open(config_path, 'r') as f:
            self._config = yaml.safe_load(f)
        
        # Credentials from environment (no hard failure at import: components
        # that need a credential check for it themselves at use time)
        self.alpaca_api_key_id = os.getenv("ALPACA_API_KEY_ID")
        self.alpaca_api_secret_key = os.getenv("ALPACA_API_SECRET_KEY")
        self.nvidia_api_key = os.getenv("NVIDIA_API_KEY")
        
        if not self.alpaca_api_key_id or not self.alpaca_api_secret_key:
            raise ValueError("Alpaca API keys not found in environment variables")

    def require(self, *names):
        """Raise if any named env credential is missing (components opt in)."""
        missing = [n for n in names if not os.getenv(n)]
        if missing:
            raise ValueError(f"Missing environment credentials: {', '.join(missing)}")

    def __getattr__(self, name):
        # Allow access to config keys as attributes, e.g., config.symbols
        if name in self._config:
            return self._config[name]
        raise AttributeError(f"'Config' object has no attribute '{name}'")

# Singleton instance
config = Config()
