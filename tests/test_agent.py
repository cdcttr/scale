import json
import pytest
from symphony.agent.claude import parse_stream_event, TurnResult, TokenUsage

def _event(type_: str, **kwargs) -> str:
    return json.dumps({"type": type_, **kwargs})

def test_parse_assistant_event_returns_none():
    line = _event("assistant", message={"content": []})
    result = parse_stream_event(line)
    assert result is None

def test_parse_success_result():
    line = _event(
        "result",
        subtype="success",
        result="Done.",
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    result = parse_stream_event(line)
    assert isinstance(result, TurnResult)
    assert result.success is True
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 50

def test_parse_error_result():
    line = _event("result", subtype="error", result="Something failed.")
    result = parse_stream_event(line)
    assert isinstance(result, TurnResult)
    assert result.success is False
    assert result.usage is None

def test_parse_unknown_event_returns_none():
    line = _event("system", subtype="init")
    result = parse_stream_event(line)
    assert result is None

def test_token_usage_total():
    usage = TokenUsage(input_tokens=200, output_tokens=80)
    assert usage.total == 280
