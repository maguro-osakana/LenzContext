"""Minimal Chat Completions vision adapter with at most one retry."""

import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import ValidationError

from ..config import Prompts, Settings
from ..models import Address, AnalysisResult
from .base import LLMError

LOG = logging.getLogger(__name__)
Transport = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]


class TransportError(LLMError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        # Do not forward images or credentials to redirects from the configured endpoint.
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            LOG.debug("POST %s -> HTTP %s", url, getattr(response, "status", "unknown"))
            return json.load(response)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        LOG.debug("POST %s -> HTTP %s", url, status)
        raise TransportError(f"LLM API returned HTTP {status}", status) from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise TransportError("LLM API connection failed or timed out") from None
    except (ValueError, UnicodeError):
        raise TransportError("LLM API returned invalid JSON") from None


def _log_response(response: Any) -> None:
    """Log optional diagnostics without making them required response fields."""
    if not LOG.isEnabledFor(logging.DEBUG):
        return
    data = response if isinstance(response, dict) else {}
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}

    def count(values: dict, key: str) -> int | str:
        value = values.get(key)
        return value if type(value) is int and value >= 0 else "unknown"

    LOG.debug("LLM usage: prompt=%s completion=%s reasoning=%s total=%s",
              count(usage, "prompt_tokens"), count(usage, "completion_tokens"),
              count(details, "reasoning_tokens"), count(usage, "total_tokens"))
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            reasoning = message.get("reasoning")
            if not isinstance(reasoning, str) or not reasoning:
                reasoning = message.get("reasoning_content")
            LOG.debug("LLM reasoning:\n%s", reasoning if isinstance(reasoning, str) else "unknown")


def parse_response(response: dict[str, Any]) -> AnalysisResult:
    _log_response(response)
    try:
        choice = response["choices"][0]
        if choice.get("finish_reason") not in {None, "stop"}:
            raise ValueError("incomplete response")
        message = choice["message"]
        refusal = message.get("refusal")
        if refusal:
            LOG.debug("LLM refused the request: %s", refusal)
            raise ValueError("refusal")
        content = message["content"]
        if not isinstance(content, str):
            raise ValueError("missing text response")
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        raise LLMError("LLM response is not valid analysis JSON") from None
    LOG.debug("LLM assistant content: %s", content)
    try:
        return AnalysisResult.model_validate_json(content)
    except (ValueError, ValidationError):
        raise LLMError("LLM response is not valid analysis JSON") from None


def _redacted_payload(payload: dict[str, Any], image_size: int, digest: str) -> dict[str, Any]:
    """Copy the request payload for logging without embedding the JPEG bytes."""
    placeholder = f"data:image/jpeg;base64,<omitted {image_size} bytes; sha256={digest}>"
    redacted = {key: value for key, value in payload.items() if key != "messages"}
    messages = []
    for message in payload["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            content = [
                {**part, "image_url": {"url": placeholder}} if part.get("type") == "image_url" else part
                for part in content
            ]
        messages.append({**message, "content": content})
    redacted["messages"] = messages
    return redacted


class OpenAICompatibleAnalyzer:
    def __init__(self, settings: Settings, prompts: Prompts, transport: Transport = send_json):
        self.settings = settings
        self.prompts = prompts
        self.transport = transport

    def analyze(self, jpeg: bytes, address: Address | None, taken_at: str | None) -> AnalysisResult:
        system, user = self.prompts.render(address, taken_at)
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
                    }},
                ]},
            ],
        }
        if self.prompts.reasoning is not None:
            payload.update(self.prompts.reasoning.request_parameters())
        if self.settings.structured_output:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "image_analysis", "strict": True,
                                "schema": AnalysisResult.model_json_schema()},
            }
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        digest = hashlib.sha256(jpeg).hexdigest()[:16]
        LOG.debug("LLM request: url=%s model=%s structured_output=%s image=%d bytes sha256=%s",
                  self.settings.completions_url, self.settings.model,
                  "response_format" in payload, len(jpeg), digest)
        LOG.debug("LLM system prompt:\n%s", system)
        LOG.debug("LLM user prompt:\n%s", user)
        LOG.debug("LLM request payload (image bytes omitted): %s",
                  json.dumps(_redacted_payload(payload, len(jpeg), digest), ensure_ascii=False))
        for attempt in range(2):
            try:
                LOG.debug("LLM request: attempt=%d reasoning_effort=%s thinking_token_budget=%s",
                          attempt + 1, payload.get("reasoning_effort", "server default"),
                          payload.get("thinking_token_budget", "server default"))
                started = time.perf_counter()
                try:
                    response = self.transport(self.settings.completions_url, payload, headers, self.settings.timeout)
                finally:
                    LOG.debug("LLM request: attempt=%d elapsed=%.2fs", attempt + 1, time.perf_counter() - started)
                return parse_response(response)
            except TransportError as exc:
                if self.prompts.reasoning is not None and exc.status in {400, 404, 415, 422}:
                    raise LLMError(
                        f"LLM API returned HTTP {exc.status} with reasoning controls configured; "
                        "check server/model support for reasoning_effort and thinking_token_budget "
                        "and compatibility with structured output; controls were not removed"
                    ) from None
                # Common compatibility-server rejections of response_format.
                unsupported = "response_format" in payload and exc.status in {400, 404, 415, 422}
                if unsupported:
                    payload.pop("response_format")
                retryable = unsupported or exc.status is None or exc.status == 429 or (exc.status >= 500)
                if attempt or not retryable:
                    raise
                LOG.warning("LLM request failed; retrying once%s", " without structured output" if unsupported else "")
            except LLMError:
                if attempt:
                    raise
                LOG.warning("invalid LLM analysis; retrying once")
        raise LLMError("LLM analysis failed")  # Unreachable; keeps the return type explicit.
