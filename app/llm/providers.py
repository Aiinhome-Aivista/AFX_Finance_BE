"""Configurable LLM providers over plain HTTPS (no vendor SDK required)."""
import requests

from .base import LLMError, LLMProvider


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key, model, timeout, base_url=None):
        self.key, self.model = api_key, model
        self.timeout = int(timeout) if timeout else 120
        self.url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"

    def complete(self, system, user, max_tokens=1200):
        try:
            r = requests.post(self.url, timeout=self.timeout,
                              headers={"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                              json={"model": self.model, "max_tokens": max_tokens, "system": system,
                                    "messages": [{"role": "user", "content": user}]})
        except requests.RequestException as e:
            raise LLMError(f"LLM request failed: {e}")
        if r.status_code != 200:
            raise LLMError(f"LLM returned HTTP {r.status_code}.")
        try:
            return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        except (KeyError, ValueError, TypeError):
            raise LLMError("Unexpected LLM response shape.")


class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions; also any OpenAI-compatible server (vLLM, LM Studio...) via LLM_BASE_URL."""
    name = "openai"

    def __init__(self, api_key, model, timeout, base_url=None):
        self.key, self.model = api_key, model
        self.timeout = int(timeout) if timeout else 120
        self.url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"

    def complete(self, system, user, max_tokens=1200):
        headers = {"content-type": "application/json"}
        if self.key:
            headers["authorization"] = f"Bearer {self.key}"
        try:
            r = requests.post(self.url, timeout=self.timeout, headers=headers,
                              json={"model": self.model, "max_tokens": max_tokens, "temperature": 0.2,
                                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        except requests.RequestException as e:
            raise LLMError(f"LLM request failed: {e}")
        if r.status_code != 200:
            raise LLMError(f"LLM returned HTTP {r.status_code}.")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError, TypeError):
            raise LLMError("Unexpected LLM response shape.")


class OllamaProvider(LLMProvider):
    """Direct Ollama provider with native JSON mode and parameter support."""
    name = "ollama"

    def __init__(self, model, timeout, base_url=None):
        self.model = model
        self.timeout = int(timeout) if timeout else 180
        url = (base_url or "http://localhost:11434").rstrip("/")
        if not url.endswith("/api/chat"):
            url = f"{url}/api/chat"
        self.url = url

    def complete(self, system, user, max_tokens=1500):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        try:
            r = requests.post(self.url, timeout=self.timeout, json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": 0.2}
            })
        except requests.RequestException as e:
            raise LLMError(f"LLM request failed: {e}")
        if r.status_code != 200:
            raise LLMError(f"LLM returned HTTP {r.status_code}.")
        try:
            return r.json().get("message", {}).get("content", "") or ""
        except (KeyError, ValueError, TypeError):
            raise LLMError("Unexpected LLM response shape.")

    def complete_json(self, system, user, max_tokens=2500):
        messages = []
        if system:
            messages.append({"role": "system", "content": system + "\n\nRespond ONLY with a valid JSON object matching the requested schema."})
        messages.append({"role": "user", "content": user})
        try:
            r = requests.post(self.url, timeout=self.timeout, json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "format": "json",
                "options": {"num_predict": max_tokens, "temperature": 0.2}
            })
        except requests.RequestException as e:
            raise LLMError(f"LLM request failed: {e}")
        if r.status_code != 200:
            raise LLMError(f"LLM returned HTTP {r.status_code}.")
        try:
            content = r.json().get("message", {}).get("content", "") or ""
        except (KeyError, ValueError, TypeError):
            raise LLMError("Unexpected LLM response shape.")
        from .base import parse_json_object
        return parse_json_object(content)


def build_provider(cfg):
    """Return a provider from config, or None when no LLM is configured (rule-based fallback is used)."""
    p = (cfg.LLM_PROVIDER or "none").lower()
    if p in ("", "none"):
        return None
    if p == "ollama":
        return OllamaProvider(cfg.LLM_MODEL, cfg.LLM_TIMEOUT_SECONDS, cfg.LLM_BASE_URL)
    if p == "anthropic":
        return AnthropicProvider(cfg.LLM_API_KEY, cfg.LLM_MODEL, cfg.LLM_TIMEOUT_SECONDS, cfg.LLM_BASE_URL or None) if cfg.LLM_API_KEY else None
    if p == "openai":
        return OpenAIProvider(cfg.LLM_API_KEY, cfg.LLM_MODEL, cfg.LLM_TIMEOUT_SECONDS, cfg.LLM_BASE_URL or None) if cfg.LLM_API_KEY else None
    if p == "openai_compatible":
        if cfg.LLM_BASE_URL and "/api/chat" in cfg.LLM_BASE_URL:
            return OllamaProvider(cfg.LLM_MODEL, cfg.LLM_TIMEOUT_SECONDS, cfg.LLM_BASE_URL)
        return OpenAIProvider(cfg.LLM_API_KEY, cfg.LLM_MODEL, cfg.LLM_TIMEOUT_SECONDS, cfg.LLM_BASE_URL) if cfg.LLM_BASE_URL else None
    return None
