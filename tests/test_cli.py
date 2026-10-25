import pytest

from nbaforecast.cli import main


def test_help_runs():
    assert main([]) == 0


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "nbaf" in capsys.readouterr().out
