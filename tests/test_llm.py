import base64
import copy
import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from lenzcontext.config import Reasoning, Settings, load_prompts
from lenzcontext.llm.base import LLMError
from lenzcontext.llm.openai_compatible import OpenAICompatibleAnalyzer, TransportError, parse_response
from lenzcontext.models import Address


def response(analysis):
    return {"choices": [{"finish_reason": "stop", "message": {"content": analysis.model_dump_json()}}]}


def analyzer(transport, structured=False, retries=5):
    return OpenAICompatibleAnalyzer(
        Settings(api_base="http://localhost:8080/v1", model="test-vision", api_key="test-secret",
                 structured_output=structured, retries=retries),
        replace(load_prompts(Path("config/prompts.yaml")), reasoning=None), transport,
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


def test_default_five_total_attempts_after_invalid_analysis():
    calls = []
    def transport(*args):
        calls.append(1)
        return {"choices": []}
    with pytest.raises(LLMError):
        analyzer(transport).analyze(b"jpeg", None, None)
    assert len(calls) == 5


@pytest.mark.parametrize("retries", [1, 2, 5])
def test_custom_retry_limit_for_invalid_analysis(retries):
    calls = []
    def transport(*args):
        calls.append(1)
        return {"choices": []}
    with pytest.raises(LLMError):
        analyzer(transport, retries=retries).analyze(b"jpeg", None, None)
    assert len(calls) == retries


def test_zero_retries_setting_retries_until_success(analysis):
    calls = []
    def transport(*args):
        calls.append(1)
        if len(calls) <= 7:
            return {"choices": []}
        return response(analysis)
    assert analyzer(transport, retries=0).analyze(b"jpeg", None, None) == analysis
    assert len(calls) == 8


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


def test_transient_failures_use_same_retry_limit():
    calls = []
    def transport(*args):
        calls.append(1)
        raise TransportError("temporary failure", 503)
    with pytest.raises(TransportError):
        analyzer(transport, retries=2).analyze(b"jpeg", None, None)
    assert len(calls) == 2


def test_mixed_failures_share_retry_budget(analysis):
    calls = []
    def transport(*args):
        calls.append(1)
        if len(calls) == 1:
            raise TransportError("temporary failure", 503)
        if len(calls) == 2:
            return {"choices": []}
        return response(analysis)
    with pytest.raises(TransportError):
        analyzer(transport, retries=1).analyze(b"jpeg", None, None)
    assert len(calls) == 1
    calls.clear()
    with pytest.raises(LLMError):
        analyzer(transport, retries=2).analyze(b"jpeg", None, None)
    assert len(calls) == 2
    calls.clear()
    assert analyzer(transport, retries=3).analyze(b"jpeg", None, None) == analysis
    assert len(calls) == 3


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


def test_verbose_logs_redacted_llm_io(caplog, analysis):
    caplog.set_level(logging.DEBUG, logger="lenzcontext")
    jpeg = b"TOP-SECRET-IMAGE-BYTES"

    def transport(url, payload, headers, timeout):
        return response(analysis)

    result = analyzer(transport).analyze(jpeg, Address(english="Seoul", local="서울"), "2026-01-01T00:00:00")
    assert result == analysis
    assert "LLM system prompt" in caplog.text
    assert "LLM request payload (image bytes omitted)" in caplog.text
    assert "image/jpeg;base64,<omitted" in caplog.text
    assert "sha256=" in caplog.text
    assert "LLM assistant content" in caplog.text
    assert "test-secret" not in caplog.text
    assert base64.b64encode(jpeg).decode() not in caplog.text


def test_invalid_response_content_is_logged(caplog):
    caplog.set_level(logging.DEBUG, logger="lenzcontext")
    with pytest.raises(LLMError):
        parse_response({"choices": [{"message": {"content": "not JSON"}}]})
    assert "not JSON" in caplog.text


def test_response_ocr_normalized_after_raw_logging(caplog, analysis):
    caplog.set_level(logging.DEBUG, logger="lenzcontext")
    data = analysis.model_dump()
    data["ocr"]["text"] = "  서울역\r\nWelcome\t出口　 "
    raw = json.dumps(data, ensure_ascii=False)
    result = parse_response({"choices": [{"message": {"content": raw}}]})
    assert result.ocr.text == "서울역 Welcome 出口"
    assert raw in caplog.text


@pytest.mark.parametrize("enabled,token_budget,expected", [
    (False, 4096, {"reasoning_effort": "none"}),
    (True, 512, {"reasoning_effort": "low", "thinking_token_budget": 512}),
    (True, 1024, {"reasoning_effort": "low", "thinking_token_budget": 1024}),
    (True, 4096, {"reasoning_effort": "low", "thinking_token_budget": 4096}),
])
def test_reasoning_request(enabled, token_budget, expected, analysis):
    def transport(url, payload, *args):
        controls = {k: v for k, v in payload.items() if k in {"reasoning_effort", "thinking_token_budget"}}
        assert controls == expected
        return response(analysis)
    client = analyzer(transport)
    client.prompts = replace(client.prompts, reasoning=Reasoning(enabled=enabled, token_budget=token_budget))
    assert client.analyze(b"jpeg", None, None) == analysis


def test_omitted_reasoning_sends_no_controls(analysis):
    def transport(url, payload, *args):
        assert "reasoning_effort" not in payload
        assert "thinking_token_budget" not in payload
        return response(analysis)
    assert analyzer(transport).analyze(b"jpeg", None, None) == analysis


@pytest.mark.parametrize("status", [400, 404, 415, 422])
@pytest.mark.parametrize("structured", [False, True])
def test_reasoning_rejection_does_not_remove_controls_or_retry(status, structured):
    calls = []
    def transport(url, payload, *args):
        calls.append(copy.deepcopy(payload))
        raise TransportError("rejected", status)
    client = analyzer(transport, structured)
    client.prompts = replace(client.prompts, reasoning=Reasoning(enabled=True))
    with pytest.raises(LLMError, match="check server/model support"):
        client.analyze(b"jpeg", None, None)
    assert len(calls) == 1
    assert calls[0]["thinking_token_budget"] == 512


@pytest.mark.parametrize("key", ["reasoning", "reasoning_content"])
def test_reasoning_and_usage_logging(key, caplog, analysis):
    caplog.set_level(logging.DEBUG, logger="lenzcontext")
    result = response(analysis)
    result["choices"][0]["message"][key] = "Inspecting visible text."
    result["usage"] = {"prompt_tokens": 1375, "completion_tokens": 2798, "total_tokens": 4173,
                       "completion_tokens_details": {"reasoning_tokens": 2697}}
    assert parse_response(result) == analysis
    assert "Inspecting visible text." in caplog.text
    assert "prompt=1375 completion=2798 reasoning=2697 total=4173" in caplog.text


@pytest.mark.parametrize("usage", [None, {}, {"completion_tokens_details": None}, [],
                                  {"prompt_tokens": "invalid", "completion_tokens_details": []}])
def test_optional_usage_is_not_required(usage, caplog, analysis):
    caplog.set_level(logging.DEBUG, logger="lenzcontext")
    result = response(analysis)
    result["usage"] = usage
    assert parse_response(result) == analysis
    assert "prompt=unknown completion=unknown reasoning=unknown total=unknown" in caplog.text


def test_zero_reasoning_and_info_logs(caplog, analysis):
    result = response(analysis)
    result["choices"][0]["message"]["reasoning"] = "Private reasoning text"
    result["usage"] = {"completion_tokens_details": {"reasoning_tokens": 0}}
    caplog.set_level(logging.INFO, logger="lenzcontext")
    assert parse_response(result) == analysis
    assert "Private reasoning text" not in caplog.text
    assert "LLM usage" not in caplog.text
    caplog.set_level(logging.DEBUG, logger="lenzcontext")
    parse_response(result)
    assert "reasoning=0" in caplog.text


def test_failed_response_logs_reasoning_before_validation(caplog, analysis):
    caplog.set_level(logging.DEBUG, logger="lenzcontext")
    result = response(analysis)
    result["choices"][0].update(finish_reason="length")
    result["choices"][0]["message"]["reasoning"] = "Unfinished reasoning"
    with pytest.raises(LLMError):
        parse_response(result)
    assert "Unfinished reasoning" in caplog.text


def test_retry_logs_each_attempt_and_preserves_controls(monkeypatch, caplog, analysis):
    caplog.set_level(logging.DEBUG, logger="lenzcontext")
    clock = iter([10.0, 12.0, 20.0, 23.0])
    monkeypatch.setattr("lenzcontext.llm.openai_compatible.time.perf_counter", lambda: next(clock))
    calls = []
    def transport(url, payload, *args):
        calls.append(copy.deepcopy(payload))
        if len(calls) == 1:
            raise TransportError("unavailable", 503)
        return response(analysis)
    client = analyzer(transport)
    client.prompts = replace(client.prompts, reasoning=Reasoning(enabled=True))
    assert client.analyze(b"jpeg", None, None) == analysis
    assert calls[0] == calls[1]
    assert "attempt=1 elapsed=2.00s" in caplog.text
    assert "attempt=2 elapsed=3.00s" in caplog.text
    assert "reasoning_effort=low thinking_token_budget=512" in caplog.text
