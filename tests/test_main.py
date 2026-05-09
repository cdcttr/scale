from __future__ import annotations
import sys
import pytest


# --- Parser defaults ---

def test_run_workflow_defaults_to_workflow_md():
    from scale.main import _build_parser
    args = _build_parser().parse_args(["run"])
    assert args.workflow == "WORKFLOW.md"


def test_triage_workflow_defaults_to_workflow_md():
    from scale.main import _build_parser
    args = _build_parser().parse_args(["triage"])
    assert args.workflow == "WORKFLOW.md"


def test_plan_workflow_defaults_to_workflow_md():
    from scale.main import _build_parser
    args = _build_parser().parse_args(["plan", "--issue", "1"])
    assert args.workflow == "WORKFLOW.md"


def test_clean_workflow_defaults_to_workflow_md():
    from scale.main import _build_parser
    args = _build_parser().parse_args(["clean"])
    assert args.workflow == "WORKFLOW.md"


def test_explicit_workflow_path_overrides_default():
    from scale.main import _build_parser
    args = _build_parser().parse_args(["triage", "path/to/OTHER.md"])
    assert args.workflow == "path/to/OTHER.md"


# --- Missing file exits with error ---

def test_missing_default_workflow_triage_exits_with_code_1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["scale", "triage"])
    from scale.main import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    assert "WORKFLOW.md" in capsys.readouterr().err


def test_missing_default_workflow_run_exits_with_code_1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["scale", "run"])
    from scale.main import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    assert "WORKFLOW.md" in capsys.readouterr().err


def test_missing_default_workflow_plan_exits_with_code_1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["scale", "plan", "--issue", "1"])
    from scale.main import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    assert "WORKFLOW.md" in capsys.readouterr().err


def test_missing_default_workflow_clean_exits_with_code_1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["scale", "clean"])
    from scale.main import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    assert "WORKFLOW.md" in capsys.readouterr().err


def test_explicit_missing_workflow_exits_with_code_1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["scale", "triage", "NO_SUCH.md"])
    from scale.main import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    assert "NO_SUCH.md" in capsys.readouterr().err
