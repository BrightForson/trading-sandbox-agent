import os
from dotenv import load_dotenv
load_dotenv()  # load environment variables from .env
import openai
from bot.errors import ModelError

class ModelClient:
    def __init__(self):
        self.api_key = os.getenv("NVIDIA_API_KEY")
        if not self.api_key:
            raise ModelError("NVIDIA_API_KEY not found in environment variables")

        # Model chain verified live on build.nvidia.com (2026-09-03):
        # primary -> fallback (all tested working; deepseek-v4-pro & glm-5.2 are EOL/410)
        self.primary_model = "nvidia/nemotron-3-super-120b-a12b"
        self.fallback_model = "minimaxai/minimax-m3"

        # Configure OpenAI client to point to NVIDIA endpoint
        self.client = openai.OpenAI(
            api_key=self.api_key,
            base_url="https://integrate.api.nvidia.com/v1",
            timeout=30
        )

    def generate_text(self, prompt, max_tokens=500, temperature=0.7):
        """
        Generate text using the model with fallback.
        :param prompt: input prompt
        :param max_tokens: maximum tokens to generate
        :param temperature: sampling temperature
        :return: generated text string
        """
        # Try primary model first
        try:
            response = self.client.chat.completions.create(
                model=self.primary_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature
            )
            return response.choices[0].message.content
        except Exception as e:
            # Log the error (in real code, we would use logging)
            print(f"Primary model ({self.primary_model}) failed: {e}")
            # Try fallback model
            try:
                response = self.client.chat.completions.create(
                    model=self.fallback_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    temperature=temperature
                )
                return response.choices[0].message.content
            except Exception as e2:
                raise ModelError(f"Both primary and fallback models failed. Primary: {e}, Fallback: {e2}")