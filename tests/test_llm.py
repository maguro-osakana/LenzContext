import base64
import copy
import json
from pathlib import Path

import pytest

from lenzcontext.config import Settings, load_prompts
from lenzcontext.llm.base import LLMError
from lenzcontext.llm.openai_compatible import OpenAICompatibleAnalyzer, TransportError, parse_response
from lenzcontext.models import Address


def response(analysis):
    return {"choices": [{"finish_reason": "stop", "message": {"content": analysis.model_dump_json()}}]}


def analyzer(transport, structured=False):
    return OpenAICompatibleAnalyzer(
        Settings(api_base="http://localhost:8080/v1", model="test-vision", api_key="test-secret",
                 structured_output=structured),
        load_prompts(Path("config/prompts.yaml")), transport,
    )


def test_payload_exact_bytes_and_metadata_allowlist(make_jpeg, analysis):
    original = make_jpeg().read_bytes()
    calls = []
    def transport(url, payload, headers, timeout):
        calls.append(payload)
        assert url.endswith("/v1/chat/completions")
        assert headers["Authorization"] == "Bearer test-secret"
        parts = payload["messages"][1]["content"]
        assert base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1]) == original
        context = parts[0]["text"]
        assert "English: Seoul" in context and "Local: 서울" in context
        assert "Capture time: 2026-01-01T00:00:00" in context
        for forbidden in ("latitude", "longitude", "geoname_id", "country_code", "admin1_code", "population"):
            assert forbidden not in json.dumps(payload)
        assert "response_format" not in payload
        return response(analysis)
    result = analyzer(transport).analyze(original, Address(english="Seoul", local="서울"), "2026-01-01T00:00:00")
    assert result == analysis
    assert len(calls) == 1


def test_validation_retry_once(analysis):
    calls = []
    def transport(*args):
        calls.append(1)
        return {"choices": [{"message": {"content": "not JSON"}}]} if len(calls) == 1 else response(analysis)
    assert analyzer(transport).analyze(b"jpeg", None, None) == analysis
    assert len(calls) == 2


def test_exhausted_retry():
    calls = []
    def transport(*args):
        calls.append(1)
        return {"choices": []}
    with pytest.raises(LLMError):
        analyzer(transport).analyze(b"jpeg", None, None)
    assert len(calls) == 2


def test_structured_output_fallback(analysis):
    calls = []
    def transport(url, payload, *args):
        calls.append(copy.deepcopy(payload))
        if len(calls) == 1:
            raise TransportError("LLM API returned HTTP 400", 400)
        return response(analysis)
    assert analyzer(transport, structured=True).analyze(b"jpeg", None, None) == analysis
    assert calls[0]["response_format"]["json_schema"]["strict"] is True
    assert "response_format" not in calls[1]


def test_auth_error_no_retry_or_key_logging(caplog):
    calls = []
    def transport(*args):
        calls.append(1)
        raise TransportError("LLM API returned HTTP 401", 401)
    with pytest.raises(LLMError, match="401"):
        analyzer(transport).analyze(b"jpeg", None, None)
    assert len(calls) == 1
    assert "test-secret" not in caplog.text


@pytest.mark.parametrize("result", [None, {}, {"choices": []}, {"choices": [{"message": {"content": "{}"}}]}])
def test_invalid_responses(result):
    with pytest.raises(LLMError):
        parse_response(result)


@pytest.mark.parametrize("status", [None, 429, 503])
def test_transient_failure_retry(analysis, status):
    calls = []
    def transport(*args):
        calls.append(1)
        if len(calls) == 1:
            raise TransportError("temporary failure", status)
        return response(analysis)
    assert analyzer(transport).analyze(b"jpeg", None, None) == analysis
    assert len(calls) == 2


def test_http_transport_serializes_request_and_sanitizes_errors(monkeypatch, analysis):
    import io
    from unittest.mock import Mock
    from urllib.error import HTTPError, URLError
    from lenzcontext.llm.openai_compatible import send_json

    opener = Mock()
    opener.open.return_value = io.BytesIO(json.dumps(response(analysis)).encode())
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.build_opener", lambda *args: opener)
    assert send_json("http://localhost/v1/chat/completions", {"model": "test"},
                     {"Authorization": "Bearer secret"}, 30) == response(analysis)
    request = opener.open.call_args.args[0]
    assert request.method == "POST"
    assert json.loads(request.data) == {"model": "test"}
    assert request.get_header("Authorization") == "Bearer secret"
    assert opener.open.call_args.kwargs["timeout"] == 30
    opener.open.side_effect = HTTPError("http://localhost", 401, "secret", {}, io.BytesIO(b"secret"))
    with pytest.raises(TransportError) as raised:
        send_json("http://localhost", {}, {}, 30)
    assert str(raised.value) == "LLM API returned HTTP 401"
    opener.open.side_effect = URLError("secret")
    with pytest.raises(TransportError) as raised:
        send_json("http://localhost", {}, {}, 30)
    assert "secret" not in str(raised.value)


def test_incomplete_and_refused_responses(analysis):
    incomplete = response(analysis)
    incomplete["choices"][0]["finish_reason"] = "length"
    with pytest.raises(LLMError):
        parse_response(incomplete)
    refused = response(analysis)
    refused["choices"][0]["message"]["refusal"] = "refused"
    with pytest.raises(LLMError):
        parse_response(refused)
