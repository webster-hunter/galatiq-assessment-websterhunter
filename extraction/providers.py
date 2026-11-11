"""LLM providers: messages plus a JSON schema in, a parsed JSON object out.

Two implementations of one method, complete(messages, schema) -> dict:

    Grok    xAI's chat-completions API with structured outputs. The model and endpoint are
            settings; another provider is one more class with the same complete() method.
    Replay  returns recorded responses, so the repo runs and tests with no key. Given a
            live provider, it records whatever it hasn't seen.

Standard library only (urllib), so the normalizer's no-dependency install still holds.
"""
import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol


class Provider(Protocol):
    def complete(self, messages: list[dict], schema: dict) -> dict: ...


class ProviderError(RuntimeError):
    """The model call failed or its reply can't be used: HTTP error, truncation, refusal, not JSON."""


class Grok:
    """Grok on xAI: chat completions with a strict JSON schema (response_format json_schema).

    Args:
        model: Model name; check the provider's current models page, the README's grok-3 is dated.
        base_url: The API root.
        api_key: Defaults to the XAI_API_KEY environment variable.
        timeout: Seconds per request.
    """

    def __init__(self, model: str = "grok-4", base_url: str = "https://api.x.ai/v1",
                 api_key: str | None = None, timeout: float = 60):
        self.model, self.base_url, self.timeout = model, base_url.rstrip("/"), timeout
        self.api_key = api_key or os.environ.get("XAI_API_KEY")
        if not self.api_key:
            raise ProviderError("no API key: set XAI_API_KEY (see .env.example) or pass api_key")

    def complete(self, messages: list[dict], schema: dict) -> dict:
        """Send one request and return the model's JSON object.

        Raises:
            ProviderError: HTTP failure, a truncated or refused reply, or content that isn't a JSON object.
        """
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,  # transcription, not creativity: same text, same answer as far as possible
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "invoice", "strict": True, "schema": schema}},
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                reply = json.load(r)
        except urllib.error.HTTPError as e:
            raise ProviderError(f"HTTP {e.code}: {e.read()[:500]!r}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise ProviderError(f"request failed: {e}") from e
        choice = reply["choices"][0]
        # A cut-off reply can still be valid JSON with fields missing: refuse it rather than load half an invoice.
        if choice.get("finish_reason") != "stop":
            raise ProviderError(f"reply not complete: finish_reason={choice.get('finish_reason')!r}")
        msg = choice["message"]
        if msg.get("refusal"):
            raise ProviderError(f"model refused: {msg['refusal']}")
        return _json_object(msg.get("content"))


def _json_object(content) -> dict:
    try:
        out = json.loads(content)
    except (TypeError, json.JSONDecodeError) as e:
        raise ProviderError(f"reply is not JSON: {str(content)[:200]!r}") from e
    if not isinstance(out, dict):
        raise ProviderError(f"reply is a JSON {type(out).__name__}, not an object")
    return out


class Replay:
    """Replays recorded responses from a folder, one JSON file per request.

    A request is keyed by a hash of its messages and schema, so a changed prompt or file misses
    instead of replaying a stale answer. The model isn't in the key; the file records which
    model produced it.

    Args:
        folder: Where recordings live.
        live: A provider to call and record on a miss. None: a miss raises ProviderError.
    """

    def __init__(self, folder: str | Path, live: Provider | None = None):
        self.folder, self.live = Path(folder), live

    def complete(self, messages: list[dict], schema: dict) -> dict:
        key = hashlib.sha256(json.dumps([messages, schema], sort_keys=True).encode()).hexdigest()[:16]
        path = self.folder / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["response"]
        if self.live is None:
            raise ProviderError(f"no recording {path.name}; run once with an API key to record it")
        response = self.live.complete(messages, schema)
        self.folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": getattr(self.live, "model", None), "messages": messages,
                                    "response": response}, indent=2), encoding="utf-8")
        return response
