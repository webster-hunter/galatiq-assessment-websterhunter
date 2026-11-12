"""LLM providers: messages plus a JSON schema in, a parsed JSON object out.

Two implementations of one method, complete(messages, schema) -> dict:

    Grok    xAI's chat-completions API with structured outputs, and function calling (chat()). The model and endpoint are
            settings; another provider is one more class with the same complete() method.
            Temporary failures (timeouts, rate limits, server errors) are retried with backoff.
    Replay  returns recorded responses, so the repo runs and tests with no key. Given a
            live provider, it records whatever it hasn't seen.

Standard library only (urllib), so the normalizer's no-dependency install still holds.
"""
import hashlib
import json
import os
import random
import time
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
        attempts: Total tries for one request, counting the first. Only temporary failures are
            retried (see RETRY_STATUS); a bad key or a malformed request fails at once.
        base_delay: Backoff unit in seconds. The wait before retry n is a random time between zero
            and base_delay * 2**n (capped at max_delay). The randomness ("jitter") matters when
            many invoices hit a rate limit together: without it they would all retry at the same
            instant and hit it again.
        max_delay: Longest single wait, in seconds.
        sleep: The wait function; tests pass a stub so they don't actually wait.
    """

    # Worth retrying: request timeout, rate limit, and server-side errors. Anything else (400 bad
    # request, 401 bad key, 404 unknown model) will fail the same way every time.
    RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})

    def __init__(self, model: str = "grok-4", base_url: str = "https://api.x.ai/v1",
                 api_key: str | None = None, timeout: float = 60, attempts: int = 3,
                 base_delay: float = 1.0, max_delay: float = 20.0, sleep=time.sleep):
        self.model, self.base_url, self.timeout = model, base_url.rstrip("/"), timeout
        self.attempts, self.base_delay, self.max_delay, self._sleep = attempts, base_delay, max_delay, sleep
        self.api_key = api_key or os.environ.get("XAI_API_KEY")
        if not self.api_key:
            raise ProviderError("no API key: set XAI_API_KEY (see .env.example) or pass api_key")

    def complete(self, messages: list[dict], schema: dict) -> dict:
        """Send one request and return the model's JSON object.

        Raises:
            ProviderError: HTTP failure (after retries, if it was temporary), a reply in an
                unexpected shape, a truncated or refused reply, or content that isn't a JSON object.
        """
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,  # transcription, not creativity: same text, same answer as far as possible
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "invoice", "strict": True, "schema": schema}},
        }
        reply = self._post(body)
        try:
            choice = reply["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            # An error body, or no choices: still the provider's failure, so it costs one file, not the batch.
            raise ProviderError(f"unexpected reply shape: {str(reply)[:200]!r}") from e
        # A cut-off reply can still be valid JSON with fields missing: refuse it rather than load half an invoice.
        if choice.get("finish_reason") != "stop":
            raise ProviderError(f"reply not complete: finish_reason={choice.get('finish_reason')!r}")
        if msg.get("refusal"):
            raise ProviderError(f"model refused: {msg['refusal']}")
        return _json_object(msg.get("content"))

    def chat(self, messages: list[dict], schema: dict, tools: list[dict]) -> dict:
        """One turn of a conversation in which the model may call tools.

        This is function calling. The request lists the tools (name, description, a JSON schema
        for the arguments). The model answers in one of two ways:

            it asks for tools   the reply has tool_calls and no content. The caller runs them,
                                appends the results to the conversation, and calls chat() again.
            it answers          the reply has content, a JSON object that fits `schema`.

        The model never runs anything. It only names a tool and its arguments; the caller decides
        whether and how to run it.

        Args:
            messages: The conversation so far, including any earlier tool calls and their results.
            schema: The JSON schema the final answer must fit.
            tools: Tool definitions, each {"name", "description", "parameters"}.

        Returns:
            {"tool_calls": [{"id", "name", "arguments"}], "message": the assistant message to
            append before the tool results} or {"content": the answer, "tool_calls": []}.

        Raises:
            ProviderError: As complete(), plus tool arguments that aren't a JSON object.
        """
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "tools": [{"type": "function", "function": t} for t in tools],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "answer", "strict": True, "schema": schema}},
        }
        reply = self._post(body)
        try:
            choice = reply["choices"][0]
            msg = choice["message"]
            calls = msg.get("tool_calls") or []
            parsed = [{"id": c["id"], "name": c["function"]["name"], "arguments": c["function"]["arguments"]} for c in calls]
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise ProviderError(f"unexpected reply shape: {str(reply)[:200]!r}") from e
        if msg.get("refusal"):
            raise ProviderError(f"model refused: {msg['refusal']}")
        if parsed:
            for call in parsed:
                call["arguments"] = _json_object(call["arguments"])  # the API sends arguments as a JSON string
            # Kept as the API sent it, minus fields we don't send back: it must be appended to the
            # conversation, ahead of the tool results, for the next turn to be accepted.
            return {"tool_calls": parsed, "message": {"role": "assistant", "content": msg.get("content"), "tool_calls": calls}}
        if choice.get("finish_reason") != "stop":
            raise ProviderError(f"reply not complete: finish_reason={choice.get('finish_reason')!r}")
        return {"content": _json_object(msg.get("content")), "tool_calls": []}


    def _post(self, body: dict):
        """POST the request, retrying temporary failures with jittered exponential backoff.

        Returns:
            The decoded JSON reply.

        Raises:
            ProviderError: A permanent failure, or a temporary one that outlasted every attempt.
        """
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        for attempt in range(self.attempts):
            retry_after = None
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                problem = f"HTTP {e.code}: {e.read()[:500]!r}"
                if e.code not in self.RETRY_STATUS:
                    raise ProviderError(problem) from e
                retry_after = _seconds(e.headers.get("Retry-After") if e.headers else None)
            except (urllib.error.URLError, TimeoutError) as e:  # no connection, or no answer in time
                problem = f"request failed: {e}"
            except json.JSONDecodeError as e:
                raise ProviderError(f"reply is not JSON: {e}") from e
            if attempt + 1 < self.attempts:
                self._sleep(self._delay(attempt, retry_after))
        raise ProviderError(f"{problem} (gave up after {self.attempts} attempts)")

    def _delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait before the retry that follows `attempt` (0 for the first try).

        Full jitter: a random time between zero and the backoff ceiling, which doubles each
        attempt. When the server names a wait (Retry-After), that is honoured as a minimum, with
        a little jitter on top.
        """
        ceiling = min(self.max_delay, self.base_delay * 2 ** attempt)
        if retry_after is not None:
            return min(self.max_delay, retry_after) + random.uniform(0, self.base_delay)
        return random.uniform(0, ceiling)


def _seconds(value) -> float | None:
    """A Retry-After header as seconds, or None if absent or not a plain number (it may be a date)."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


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
    instead of replaying a stale answer. The messages hold the document's text, so for PDFs the
    key depends on the text extractor's exact output: that is why pymupdf is pinned to one
    version in pyproject.toml.

    The model isn't in the key, so someone with no key can replay whatever was recorded. The
    file records which model produced it, and with a live provider a recording made by a
    different model is not replayed: it is recorded again, so switching models never serves
    the old model's answers.

    Args:
        folder: Where recordings live.
        live: A provider to call and record on a miss. None: a miss raises ProviderError.
    """

    def __init__(self, folder: str | Path, live: Provider | None = None):
        self.folder, self.live = Path(folder), live

    def complete(self, messages: list[dict], schema: dict) -> dict:
        return self._replay([messages, schema], messages, lambda: self.live.complete(messages, schema))

    def chat(self, messages: list[dict], schema: dict, tools: list[dict]) -> dict:
        """Replay or record one tool-calling turn. The tools are part of the key: change one and the turn is recorded again."""
        return self._replay([messages, schema, tools], messages, lambda: self.live.chat(messages, schema, tools))

    def _replay(self, key_parts: list, messages: list[dict], call) -> dict:
        key = hashlib.sha256(json.dumps(key_parts, sort_keys=True).encode()).hexdigest()[:16]
        path = self.folder / f"{key}.json"
        if path.exists():
            recorded = json.loads(path.read_text(encoding="utf-8"))
            live_model = getattr(self.live, "model", None)
            if live_model is None or recorded.get("model") in (None, live_model):
                return recorded["response"]
        if self.live is None:
            raise ProviderError(f"no recording {path.name}; run once with an API key to record it")
        response = call()
        self.folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": getattr(self.live, "model", None), "messages": messages,
                                    "response": response}, indent=2), encoding="utf-8")
        return response
