"""Minimal Chat Completions vision adapter with at most one retry."""

import base64
import json
import logging
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
            return json.load(response)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise TransportError(f"LLM API returned HTTP {status}", status) from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise TransportError("LLM API connection failed or timed out") from None
    except (ValueError, UnicodeError):
        raise TransportError("LLM API returned invalid JSON") from None


def parse_response(response: dict[str, Any]) -> AnalysisResult:
    try:
        choice = response["choices"][0]
        if choice.get("finish_reason") not in {None, "stop"}:
            raise ValueError("incomplete response")
        message = choice["message"]
        if message.get("refusal"):
            raise ValueError("refusal")
        content = message["content"]
        if not isinstance(content, str):
            raise ValueError("missing text response")
        return AnalysisResult.model_validate_json(content)
    except (KeyError, IndexError, TypeError, AttributeError, ValueError, ValidationError):
        raise LLMError("LLM response is not valid analysis JSON") from None


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
        if self.settings.structured_output:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "image_analysis", "strict": True,
                                "schema": AnalysisResult.model_json_schema()},
            }
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        for attempt in range(2):
            try:
                response = self.transport(self.settings.completions_url, payload, headers, self.settings.timeout)
                return parse_response(response)
            except TransportError as exc:
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
