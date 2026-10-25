from pathlib import Path

from nbaforecast import config


def test_default_local_data_dir_is_outside_repo(monkeypatch):
    monkeypatch.delenv("NBAF_DATA_DIR", raising=False)
    path = config.local_data_dir()
    assert path == Path.home() / "Desktop" / "nbaforecast"
    assert config.REPO_ROOT not in path.parents


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("NBAF_DATA_DIR", str(tmp_path))
    assert config.local_data_dir() == tmp_path


def test_published_dir_is_inside_repo():
    assert config.PUBLISHED_DIR == config.REPO_ROOT / "data" / "published"
