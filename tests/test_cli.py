"""The command line."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from acptui.cli import main
from acptui.setup import parse_option


if TYPE_CHECKING:
    from pathlib import Path


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.startswith("acptui ")


def test_agents_and_config(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[agents.mine]\ncommand = "my-agent"\n', encoding="utf-8")
    main(["--config", str(config), "agents"])
    out = capsys.readouterr().out
    assert "demo" in out
    assert "mine" in out
    main(["--config", str(config), "config"])
    assert capsys.readouterr().out.strip() == str(config)


def test_options_command(capsys: pytest.CaptureFixture[str], workdir: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--cwd", str(workdir), "options", "demo"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "mode = ask  (ask, auto)" in out
    assert "thinking = true  (true, false)" in out
    assert "/tour" in out


def test_bad_input(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("nope = [", encoding="utf-8")
    with pytest.raises(SystemExit, match="invalid config file"):
        main(["--config", str(bad), "agents"])
    with pytest.raises(SystemExit, match="not a directory"):
        main(["--cwd", str(tmp_path / "missing"), "agents"])
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        main(["--config", str(tmp_path / "none.toml"), "-o", "novalue", "run", "demo"])


def test_registry_agents_take_no_arguments(tmp_path: Path) -> None:
    config = str(tmp_path / "none.toml")
    with pytest.raises(SystemExit, match="registry agent"):
        main(["--config", config, "run", "demo", "--flag"])
    with pytest.raises(SystemExit, match="usage"):
        main(["--config", config, "run", "demo", "--resume"])


def test_parse_option() -> None:
    assert parse_option("model=opus") == ("model", "opus")
    assert parse_option("thinking=False") == ("thinking", False)
