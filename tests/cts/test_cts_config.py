"""Tests for ci_truth_serum/_cts_config.py — the reader of `.ci-truth-serum.toml`,
which gives each aggregated check its flags.

Each refusal case is a file that would otherwise configure nothing while the run
looks configured: a misspelled check, an unknown key, or a value of the wrong type.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("_cts_config.py", "_cts_config")

MODULES = {"check_retry_loop", "check_curl_retry"}


def _config(tmp_path, text: str):
    (tmp_path / mod.CONFIG_FILE).write_text(text, encoding="utf-8")
    return mod.check_args(MODULES, tmp_path)


def test_no_file_gives_no_flags(tmp_path) -> None:
    assert mod.check_args(MODULES, tmp_path) == {}


def test_a_table_names_its_check_by_hook_id_or_module(tmp_path) -> None:
    flags = _config(
        tmp_path,
        '[checks.check-retry-loop]\nargs = ["--wrapper=retry_cmd", "--x"]\n'
        '[checks.check_curl_retry]\nargs = ["--y"]\n',
    )
    assert flags == {
        "check_retry_loop": ["--wrapper=retry_cmd", "--x"],
        "check_curl_retry": ["--y"],
    }


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ('[checks.check-retry-lop]\nargs = ["--x"]\n', "names no check"),
        ('[checks.check-retry-loop]\nflags = ["--x"]\n', "takes one key, `args`"),
        ('[checks.check-retry-loop]\nargs = "--x"\n', "must be a list of strings"),
        ("[checks.check-retry-loop]\nargs = [1]\n", "must be a list of strings"),
        (
            '[check.check-retry-loop]\nargs = ["--x"]\n',
            "unknown top-level key(s): check",
        ),
        ('checks = ["check-retry-loop"]\n', "must be a table of check tables"),
        (
            '[checks.check-retry-loop]\nargs = ["--x"]\n[checks.check_retry_loop]\nargs = []\n',
            "a second time",
        ),
        ("[checks\n", "line 1"),
    ],
)
def test_a_file_that_would_configure_nothing_is_refused(tmp_path, text, said) -> None:
    with pytest.raises(mod.ConfigError) as refused:
        _config(tmp_path, text)
    assert said in str(refused.value)
