import json
import re


class LLMError(Exception):
    """Raised when the configured LLM is unreachable or returns unusable output. Callers fall back to rules."""


class LLMProvider:
    name = "base"

    def complete(self, system: str, user: str, max_tokens: int = 1200) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def complete_json(self, system: str, user: str, max_tokens: int = 1500) -> dict:
        raw = self.complete(system + "\n\nRespond with a single JSON object and nothing else.", user, max_tokens)
        return parse_json_object(raw)


def parse_json_object(raw: str) -> dict:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    try:
        v = json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if not m:
            raise LLMError("The model did not return JSON.")
        try:
            v = json.loads(m.group(0))
        except ValueError as e:
            raise LLMError(f"The model returned invalid JSON: {e}")
    if not isinstance(v, dict):
        raise LLMError("The model returned JSON that is not an object.")
    return v
