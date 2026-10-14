"""OpenAI-compatible Chat Completions client on top of urllib."""
import json
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


class ChatClient:
    def __init__(self, cfg: dict):
        self.base = cfg["base_url"].rstrip("/")
        self.url = self.base + "/chat/completions"
        self.api_key = cfg.get("api_key")
        self.model = cfg["model"]
        self.temperature = cfg.get("temperature", 0)
        self.timeout = cfg.get("timeout", 600)

    def _request(self, url: str, body: dict | None = None) -> dict:
        if not self.api_key:
            raise LLMError("API key is empty: set llm.api_key in config.yaml (or OPENAI_API_KEY)")
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError) as e:
            detail = e.read().decode(errors="replace") if isinstance(e, urllib.error.HTTPError) else e
            raise LLMError(f"request to {url} failed: {detail}") from e

    def models(self) -> list[str]:
        return sorted(m["id"] for m in self._request(self.base + "/models").get("data", []))

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        data = self._request(self.url, {"model": self.model, "messages": messages, "tools": tools,
                                        "temperature": self.temperature})
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"unexpected response: {data}") from e
