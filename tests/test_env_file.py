import os

from tests.conftest import load_env_file


def test_env_file_fills_only_unset_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("ALREADY_SET", "from-environment")
    for name in ("FRESH", "QUOTED", "EXPORTED", "BLANK"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "ALREADY_SET=from-file\n"
        "FRESH=value=with=equals\n"
        'QUOTED="quoted value"\n'
        "export EXPORTED=yes\n"
        "BLANK=\n"
        "not a setting\n"
    )

    try:
        load_env_file(env_file)

        assert os.environ["ALREADY_SET"] == "from-environment"
        assert os.environ["FRESH"] == "value=with=equals"
        assert os.environ["QUOTED"] == "quoted value"
        assert os.environ["EXPORTED"] == "yes"
        assert "BLANK" not in os.environ
    finally:
        for name in ("FRESH", "QUOTED", "EXPORTED"):
            os.environ.pop(name, None)


def test_a_missing_env_file_is_ignored(tmp_path):
    load_env_file(tmp_path / "missing.env")
