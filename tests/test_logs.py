from __future__ import annotations
import json
import pytest
from pathlib import Path

from scale.logs.reader import LogReader, _format_event, _fmt_prefix, _key_input


# --- _key_input ---

def test_key_input_prefers_command():
    assert _key_input({"command": "ls", "file_path": "foo"}) == "ls"


def test_key_input_falls_back_to_file_path():
    assert _key_input({"file_path": "scale/main.py"}) == "scale/main.py"


def test_key_input_falls_back_to_description():
    assert _key_input({"description": "some desc"}) == "some desc"


def test_key_input_falls_back_to_query():
    assert _key_input({"query": "def foo"}) == "def foo"


def test_key_input_falls_back_to_pattern():
    assert _key_input({"pattern": "*.py"}) == "*.py"


def test_key_input_falls_back_to_first_value():
    assert _key_input({"prompt": "hello"}) == "hello"


def test_key_input_empty_returns_empty():
    assert _key_input({}) == ""


# --- _format_event ---

def _assistant(content: list[dict], usage: dict | None = None) -> dict:
    msg: dict = {"content": content}
    if usage is not None:
        msg["usage"] = usage
    return {"type": "assistant", "message": msg}


_NO_TIME_NO_TOKENS = _fmt_prefix(None, 0)
_PAD = " " * len(_NO_TIME_NO_TOKENS)


def test_format_text_block():
    event = _assistant([{"type": "text", "text": "Hello world"}])
    lines = _format_event(event, turn=1)
    assert lines == [f"[turn 1]{_NO_TIME_NO_TOKENS}TEXT  Hello world"]


def test_format_empty_text_block_skipped():
    event = _assistant([{"type": "text", "text": "   "}])
    lines = _format_event(event, turn=1)
    assert lines == []


def test_format_tool_use_bash():
    event = _assistant([{"type": "tool_use", "name": "Bash", "input": {"command": "uv run pytest -q"}}])
    lines = _format_event(event, turn=2)
    assert lines == [f"[turn 2]{_NO_TIME_NO_TOKENS}TOOL  Bash — uv run pytest -q"]


def test_format_tool_use_read():
    event = _assistant([{"type": "tool_use", "name": "Read", "input": {"file_path": "scale/main.py"}}])
    lines = _format_event(event, turn=1)
    assert lines == [f"[turn 1]{_NO_TIME_NO_TOKENS}TOOL  Read — scale/main.py"]


def test_format_multiple_content_items():
    event = _assistant([
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        {"type": "text", "text": "Done"},
    ])
    lines = _format_event(event, turn=3)
    assert lines == [
        f"[turn 3]{_NO_TIME_NO_TOKENS}TOOL  Bash — ls",
        f"[turn 3]{_PAD}TEXT  Done",
    ]


def test_format_result_success():
    event = {
        "type": "result",
        "subtype": "success",
        "num_turns": 2,
        "usage": {"input_tokens": 12400, "output_tokens": 3100},
    }
    lines = _format_event(event, turn=2)
    assert len(lines) == 1
    assert lines[0] == "[result] success after 2 turns, 12.4k tokens in, 3.1k tokens out"


def test_format_result_error():
    event = {
        "type": "result",
        "subtype": "error",
        "num_turns": 1,
        "usage": {"input_tokens": 500, "output_tokens": 100},
    }
    lines = _format_event(event, turn=1)
    assert "[result] error after 1 turns" in lines[0]


def test_format_result_missing_usage():
    event = {"type": "result", "subtype": "success", "num_turns": 1}
    lines = _format_event(event, turn=1)
    assert "[result] success after 1 turns, 0.0k tokens in, 0.0k tokens out" in lines[0]


def test_format_system_event_skipped():
    assert _format_event({"type": "system", "subtype": "init"}, turn=0) == []


def test_format_user_event_skipped():
    assert _format_event({"type": "user"}, turn=0) == []


def test_format_tool_result_event_skipped():
    assert _format_event({"type": "tool_result"}, turn=0) == []


# --- LogReader ---

def _make_log(tmp_path: Path, events: list[dict]) -> Path:
    log = tmp_path / "agent.log"
    lines = ["=" * 60, "Turn 1 — 2026-01-01", "=" * 60, "", "EVENTS:"]
    for ev in events:
        lines.append(json.dumps(ev))
    log.write_text("\n".join(lines) + "\n")
    return log


def test_log_reader_yields_tool_lines(tmp_path):
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
        ]}},
        {"type": "result", "subtype": "success", "num_turns": 1,
         "usage": {"input_tokens": 100, "output_tokens": 50}},
    ]
    log = _make_log(tmp_path, events)
    reader = LogReader(log)
    lines = list(reader.iter_formatted())
    assert any("TOOL  Bash — ls" in l for l in lines)
    assert any("[result]" in l for l in lines)


def test_log_reader_skips_non_json_lines(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("not json\n" + json.dumps({"type": "system"}) + "\n")
    reader = LogReader(log)
    lines = list(reader.iter_formatted())
    assert lines == []


def test_log_reader_turn_numbers_increment(tmp_path):
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Step one"}
        ]}},
        {"type": "user", "content": []},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Step two"}
        ]}},
        {"type": "result", "subtype": "success", "num_turns": 2,
         "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    log = _make_log(tmp_path, events)
    reader = LogReader(log)
    lines = list(reader.iter_formatted())
    assert any("[turn 1]" in l and "TEXT  Step one" in l for l in lines)
    assert any("[turn 2]" in l and "TEXT  Step two" in l for l in lines)


def test_log_reader_missing_file_raises(tmp_path):
    reader = LogReader(tmp_path / "nonexistent.log")
    with pytest.raises(FileNotFoundError):
        list(reader.iter_formatted())


# --- workspace resolution ---

def test_find_workspace_by_number(tmp_path):
    from scale.logs.reader import find_workspace
    ws = tmp_path / "owner_repo_57"
    ws.mkdir()
    result = find_workspace(tmp_path, 57)
    assert result == ws


def test_find_workspace_returns_none_when_missing(tmp_path):
    from scale.logs.reader import find_workspace
    assert find_workspace(tmp_path, 99) is None


def test_find_workspace_does_not_match_prefix_number(tmp_path):
    from scale.logs.reader import find_workspace
    (tmp_path / "owner_repo_157").mkdir()
    assert find_workspace(tmp_path, 57) is None


def test_find_archived_log(tmp_path):
    from scale.logs.reader import find_archived_log
    archive = tmp_path / "logs"
    archive.mkdir()
    log1 = archive / "57-20260101T000000.log"
    log2 = archive / "57-20260102T000000.log"
    log1.write_text("old")
    log2.write_text("new")
    result = find_archived_log(archive, 57)
    assert result == log2


def test_find_archived_log_returns_none_when_missing(tmp_path):
    from scale.logs.reader import find_archived_log
    archive = tmp_path / "logs"
    archive.mkdir()
    assert find_archived_log(archive, 57) is None


# --- main parser ---

def test_logs_parser_issue_number():
    from scale.main import _build_parser
    args = _build_parser().parse_args(["logs", "57"])
    assert args.issue == 57


def test_logs_parser_workflow_defaults():
    from scale.main import _build_parser
    args = _build_parser().parse_args(["logs", "57"])
    assert args.workflow == "WORKFLOW.md"


def test_logs_parser_all_flag():
    from scale.main import _build_parser
    args = _build_parser().parse_args(["logs", "57", "--all"])
    assert args.all is True


def test_logs_parser_archived_flag():
    from scale.main import _build_parser
    args = _build_parser().parse_args(["logs", "57", "--archived"])
    assert args.archived is True


def test_logs_parser_defaults_no_flags():
    from scale.main import _build_parser
    args = _build_parser().parse_args(["logs", "57"])
    assert args.all is False
    assert args.archived is False


# --- token extraction and prefix formatting ---

def test_fmt_prefix_static_no_tokens():
    prefix = _fmt_prefix(None, 0)
    assert " ---" in prefix
    assert "0.0k tokens" in prefix


def test_fmt_prefix_live_elapsed_and_tokens():
    prefix = _fmt_prefix(12.0, 2100)
    assert "12s" in prefix
    assert "2.1k tokens" in prefix


def test_fmt_prefix_consistent_width():
    assert len(_fmt_prefix(None, 0)) == len(_fmt_prefix(8.0, 500))
    assert len(_fmt_prefix(None, 0)) == len(_fmt_prefix(99.0, 14200))


def test_format_event_uses_total_tokens_parameter():
    event = _assistant(
        [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
        usage={"input_tokens": 1000, "output_tokens": 500},
    )
    lines = _format_event(event, turn=1, total_tokens=1500)
    assert len(lines) == 1
    assert "1.5k tokens" in lines[0]


def test_format_event_elapsed_appears_on_first_line_only():
    event = _assistant(
        [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "text", "text": "Done"},
        ],
    )
    lines = _format_event(event, turn=1, elapsed_s=8.0, total_tokens=1000)
    assert len(lines) == 2
    assert "8s" in lines[0]
    assert "1.0k tokens" in lines[0]
    assert "8s" not in lines[1]
    assert "tokens" not in lines[1]


def test_format_event_static_shows_dashes_for_time():
    event = _assistant([{"type": "text", "text": "Hello"}])
    lines = _format_event(event, turn=1, elapsed_s=None)
    assert " ---" in lines[0]


def test_format_result_with_total_wall_time():
    event = {
        "type": "result",
        "subtype": "success",
        "num_turns": 2,
        "usage": {"input_tokens": 12400, "output_tokens": 3100},
    }
    lines = _format_event(event, turn=2, total_s=94.3)
    assert len(lines) == 1
    assert lines[0].endswith(", 94s total")


def test_format_result_without_total_wall_time():
    event = {
        "type": "result",
        "subtype": "success",
        "num_turns": 2,
        "usage": {"input_tokens": 12400, "output_tokens": 3100},
    }
    lines = _format_event(event, turn=2)
    assert len(lines) == 1
    assert "total" not in lines[0]


def test_log_reader_static_shows_dashes_for_time(tmp_path):
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                "usage": {"input_tokens": 500, "output_tokens": 200},
            },
        },
        {"type": "result", "subtype": "success", "num_turns": 1,
         "usage": {"input_tokens": 500, "output_tokens": 200}},
    ]
    log = _make_log(tmp_path, events)
    reader = LogReader(log)
    lines = list(reader.iter_formatted())
    tool_line = next(l for l in lines if "TOOL" in l)
    assert " ---" in tool_line
    assert "0.7k tokens" in tool_line


def test_log_reader_static_result_has_no_total_time(tmp_path):
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}]}},
        {"type": "result", "subtype": "success", "num_turns": 1,
         "usage": {"input_tokens": 100, "output_tokens": 50}},
    ]
    log = _make_log(tmp_path, events)
    reader = LogReader(log)
    lines = list(reader.iter_formatted())
    result_line = next(l for l in lines if "[result]" in l)
    assert "total" not in result_line


def test_log_reader_shows_last_cumulative_when_turn_has_no_usage(tmp_path):
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Turn 1"}],
                                          "usage": {"input_tokens": 1000, "output_tokens": 500}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Turn 2"}]}},
        {"type": "result", "subtype": "success", "num_turns": 2,
         "usage": {"input_tokens": 1000, "output_tokens": 500}},
    ]
    log = _make_log(tmp_path, events)
    lines = list(LogReader(log).iter_formatted())
    turn1_line = next(l for l in lines if "[turn 1]" in l)
    turn2_line = next(l for l in lines if "[turn 2]" in l)
    assert "1.5k tokens" in turn1_line
    assert "1.5k tokens" in turn2_line


def test_log_reader_accumulates_tokens_across_turns(tmp_path):
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Turn 1"}],
                                          "usage": {"input_tokens": 1000, "output_tokens": 500}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Turn 2"}],
                                          "usage": {"input_tokens": 200, "output_tokens": 100}}},
        {"type": "result", "subtype": "success", "num_turns": 2,
         "usage": {"input_tokens": 1200, "output_tokens": 600}},
    ]
    log = _make_log(tmp_path, events)
    lines = list(LogReader(log).iter_formatted())
    turn1_line = next(l for l in lines if "[turn 1]" in l)
    turn2_line = next(l for l in lines if "[turn 2]" in l)
    assert "1.5k tokens" in turn1_line
    assert "1.8k tokens" in turn2_line


# --- cumulative token tracking: _format_event API ---

def test_format_event_total_tokens_overrides_event_usage():
    event = _assistant(
        [{"type": "text", "text": "Hello"}],
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    lines = _format_event(event, turn=1, total_tokens=2000)
    assert "2.0k tokens" in lines[0]


def test_format_event_total_tokens_used_when_event_has_no_usage():
    event = _assistant([{"type": "text", "text": "Hello"}])
    lines = _format_event(event, turn=1, total_tokens=1500)
    assert "1.5k tokens" in lines[0]


def test_iter_formatted_no_usage_anywhere_shows_zero(tmp_path):
    events = [
        {"type": "assistant", "message": {
            "content": [{"type": "text", "text": "Hi"}],
        }},
        {"type": "result", "subtype": "success", "num_turns": 1},
    ]
    log = _make_log(tmp_path, events)
    reader = LogReader(log)
    lines = list(reader.iter_formatted())
    turn_line = next(l for l in lines if "[turn 1]" in l)
    assert "0.0k tokens" in turn_line
