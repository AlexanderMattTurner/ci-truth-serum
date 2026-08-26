"""Tests for ci_truth_serum/check_unreset_module_state.py — the lint that flags
module-level state some function writes at runtime, in a module that declares
no reset function.

Drives ``violations()`` / ``module_bindings()`` / ``runtime_writes()`` for the
detection rules and ``main()`` for the argv/exit-code and ``--reset-name``
contract.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, load_hook

mod = load_hook("check_unreset_module_state.py", "check_unreset_module_state")


# --------------------------------------------------------------------------- #
# POSITIVE — a module-level binding some function writes, with no reset.
# --------------------------------------------------------------------------- #
def test_flags_a_written_cache_on_its_binding_line() -> None:
    text = "_cache: dict = {}\n\n\ndef remember(key, value):\n    _cache[key] = value\n"
    assert mod.violations(text) == [1]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("    global _state\n    _state = x\n", id="global-rebind"),
        pytest.param("    _state[x] = 1\n", id="subscript-store"),
        pytest.param("    del _state[x]\n", id="subscript-delete"),
        pytest.param("    _state.value = x\n", id="attribute-store"),
        pytest.param("    global _state\n    _state += 1\n", id="augmented-rebind"),
        pytest.param("    _state[x] += 1\n", id="augmented-subscript"),
        pytest.param("    _state.append(x)\n", id="mutator-append"),
        pytest.param("    _state.add(x)\n", id="mutator-add"),
        pytest.param("    _state.update({x: 1})\n", id="mutator-update"),
        pytest.param("    _state.clear()\n", id="mutator-clear"),
        pytest.param("    _state.pop(x)\n", id="mutator-pop"),
        pytest.param("    _state.setdefault(x, 1)\n", id="mutator-setdefault"),
    ],
)
def test_flags_each_runtime_write_shape(body: str) -> None:
    assert mod.violations(f"_state = 0\n\n\ndef touch(x):\n{body}") == [1]


def test_flags_a_write_from_an_async_function() -> None:
    text = "_log = []\n\n\nasync def record(x):\n    _log.append(x)\n"
    assert mod.violations(text) == [1]


def test_flags_a_write_from_a_nested_function() -> None:
    # The closure outlives `outer()`, so the write still reaches module scope.
    text = "_log = []\n\n\ndef outer():\n    def inner(x):\n        _log.append(x)\n\n    return inner\n"
    assert mod.violations(text) == [1]


# --------------------------------------------------------------------------- #
# NEGATIVE — the reset function, and the false-positive classes.
# --------------------------------------------------------------------------- #
def test_a_declared_reset_clears_the_whole_module() -> None:
    text = (
        "_cache: dict = {}\n"
        "\n"
        "\n"
        "def remember(key, value):\n"
        "    _cache[key] = value\n"
        "\n"
        "\n"
        "def _reset_process_state() -> None:\n"
        "    _cache.clear()\n"
    )
    assert mod.violations(text) == []
    assert mod.declares_reset(ast.parse(text)) is True


def test_an_import_time_constant_is_not_flagged() -> None:
    # The check's central distinction: a module-level loop runs once at
    # import, so WIRES is a mutable CONSTANT — a reset would have nothing to
    # undo. Only reads happen at runtime.
    text = (
        "WIRES = {}\n"
        "for _n in range(3):\n"
        "    WIRES[_n] = _n * 2\n"
        "\n"
        "\n"
        "def wire(n):\n"
        "    return WIRES[n]\n"
    )
    assert mod.violations(text) == []


def test_a_local_of_the_same_name_is_not_module_state() -> None:
    text = (
        "_cache = {}\n"
        "\n"
        "\n"
        "def build():\n"
        "    _cache = {}\n"
        "    _cache['k'] = 1\n"
        "    return _cache\n"
    )
    assert mod.violations(text) == []


def test_a_closure_write_to_the_parents_local_is_not_module_state() -> None:
    """The inner function alone cannot tell the two `_cache`s apart — only the
    walk that carries `build`'s locals can, which is why a nested function is
    never visited on its own."""
    text = (
        "_cache = {}\n"
        "\n"
        "\n"
        "def build():\n"
        "    _cache = {}\n"
        "\n"
        "    def fill():\n"
        "        _cache['k'] = 1\n"
        "\n"
        "    fill()\n"
        "    return _cache\n"
    )
    assert mod.violations(text) == []


def test_a_nested_function_rebinding_the_global_is_still_flagged() -> None:
    text = (
        "_settled = False\n"
        "\n"
        "\n"
        "def outer():\n"
        "    def inner():\n"
        "        global _settled\n"
        "        _settled = True\n"
        "\n"
        "    inner()\n"
    )
    assert mod.violations(text) == [1]


def test_a_parameter_of_the_same_name_is_not_module_state() -> None:
    text = "_cache = {}\n\n\ndef fill(_cache):\n    _cache['k'] = 1\n"
    assert mod.violations(text) == []


def test_an_augmented_assign_without_global_is_a_local() -> None:
    # `_count += 1` with no `global` makes `_count` local to the function — the
    # interpreter raises UnboundLocalError, so it is never a module-state write.
    assert mod.violations("_count = 0\n\n\ndef bump():\n    _count += 1\n") == []


def test_a_lock_used_as_a_context_manager_is_not_a_write() -> None:
    text = (
        "import threading\n"
        "\n"
        "_lock = threading.Lock()\n"
        "\n"
        "\n"
        "def guarded():\n"
        "    with _lock:\n"
        "        return 1\n"
    )
    assert mod.violations(text) == []


def test_a_name_only_read_is_not_flagged() -> None:
    text = "_conf = {'a': 1}\n\n\ndef setting(key):\n    return _conf[key]\n"
    assert mod.violations(text) == []


def test_a_non_mutating_method_call_is_not_a_write() -> None:
    assert (
        mod.violations("_cache = {}\n\n\ndef get(k):\n    return _cache.get(k)\n") == []
    )


def test_a_loop_target_of_the_same_name_is_not_module_state() -> None:
    text = (
        "_state = {}\n"
        "\n"
        "\n"
        "def scan(items):\n"
        "    for _state in items:\n"
        "        _state['k'] = 1\n"
    )
    assert mod.violations(text) == []


def test_a_with_binding_of_the_same_name_is_not_module_state() -> None:
    text = (
        "_handle = []\n"
        "\n"
        "\n"
        "def collect(path):\n"
        "    with open(path) as _handle:\n"
        "        _handle.append(path)\n"
    )
    assert mod.violations(text) == []


def test_an_import_inside_a_function_shadows_the_module_binding() -> None:
    text = (
        "_json = {}\n\n\ndef dump(value):\n    import _json\n\n    _json.last = value\n"
    )
    assert mod.violations(text) == []


def test_a_write_into_a_nested_container_is_not_followed() -> None:
    """`_state['inner'][k] = v` stores into the value `_state['inner']`
    returned, not into `_state` itself. The AST cannot say what that value is,
    so the check prefers the false negative."""
    text = (
        "_state = {'inner': {}}\n\n\ndef remember(k, v):\n    _state['inner'][k] = v\n"
    )
    assert mod.violations(text) == []


# --------------------------------------------------------------------------- #
# The `# allow-unreset-state:` opt-out — binding line or the line above.
# --------------------------------------------------------------------------- #
def test_annotation_on_the_binding_line_exempts_it() -> None:
    text = (
        "_cache = {}  # allow-unreset-state: the sidecar wants this across calls\n"
        "\n"
        "\n"
        "def remember(k, v):\n"
        "    _cache[k] = v\n"
    )
    assert mod.violations(text) == []


def test_annotation_on_the_line_above_also_exempts_it() -> None:
    # This check's placement rule matches the rest of the pack
    # (`annotation_window`, via `annotated_near`): the binding's own line, or
    # the line directly above it.
    text = (
        "# allow-unreset-state: the sidecar wants this across calls\n"
        "_cache = {}\n"
        "\n"
        "\n"
        "def remember(k, v):\n"
        "    _cache[k] = v\n"
    )
    assert mod.violations(text) == []


def test_a_detached_annotation_does_not_reach_the_binding() -> None:
    # A blank line ends the comment run, so an annotation written about
    # something else earlier cannot drift down onto this binding.
    text = (
        "# allow-unreset-state: written about something else\n"
        "\n"
        "_cache = {}\n"
        "\n"
        "\n"
        "def remember(k, v):\n"
        "    _cache[k] = v\n"
    )
    assert mod.violations(text) == [3]


def test_annotation_without_a_reason_does_not_exempt() -> None:
    text = "_cache = {}  # allow-unreset-state:\n\n\ndef remember(k, v):\n    _cache[k] = v\n"
    assert mod.violations(text) == [1]


# --------------------------------------------------------------------------- #
# Line-number reporting.
# --------------------------------------------------------------------------- #
def test_several_violations_report_sorted_deduplicated_lines() -> None:
    text = (
        "_a = []\n"
        "_b = {}\n"
        "\n"
        "\n"
        "def f(x):\n"
        "    _b[x] = 1\n"
        "    _a.append(x)\n"
        "\n"
        "\n"
        "def g(x):\n"
        "    _a.append(x)\n"
    )
    assert mod.violations(text) == [1, 2]


def test_a_name_bound_twice_reports_its_first_binding() -> None:
    text = "_cache = {}\n_cache = {}\n\n\ndef remember(k, v):\n    _cache[k] = v\n"
    assert mod.violations(text) == [1]


_WRITER = "\n\ndef remember(k, v):\n    _cache[k] = v\n"


@pytest.mark.parametrize(
    "head, lineno",
    [
        pytest.param(
            "import sys\n\nif sys.platform == 'linux':\n    _cache = {}\n", 4, id="if"
        ),
        pytest.param(
            "try:\n    _cache = {}\nexcept NameError:\n    _cache = {}\n", 2, id="try"
        ),
        pytest.param(
            "try:\n    _load()\nexcept OSError:\n    _cache = {}\n", 4, id="except"
        ),
    ],
)
def test_a_binding_under_a_conditional_still_counts(head: str, lineno: int) -> None:
    assert mod.violations(head + _WRITER) == [lineno]


def test_module_bindings_ignores_function_and_class_scopes() -> None:
    text = "_a = 1\n\n\ndef f():\n    b = 2\n\n\nclass C:\n    c = 3\n"
    assert mod.module_bindings(ast.parse(text)) == {"_a": 1}


def test_runtime_writes_names_the_written_binding() -> None:
    assert mod.runtime_writes(
        ast.parse("_a = []\n\n\ndef f():\n    _a.append(1)\n")
    ) == {"_a"}


# --------------------------------------------------------------------------- #
# --reset-name — the per-repo reset convention.
# --------------------------------------------------------------------------- #
def test_a_custom_reset_name_is_honoured() -> None:
    text = (
        "_cache = {}\n\n\ndef remember(k, v):\n    _cache[k] = v\n\n\n"
        "def _teardown() -> None:\n    _cache.clear()\n"
    )
    assert mod.violations(text) != []
    assert mod.violations(text, reset_name="_teardown") == []
    assert mod.declares_reset(ast.parse(text), reset_name="_teardown") is True


def test_the_default_reset_name_is_still_the_documented_convention() -> None:
    assert mod.DEFAULT_RESET_NAME == "_reset_process_state"


# --------------------------------------------------------------------------- #
# main() — the enforcement contract.
# --------------------------------------------------------------------------- #
def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_main_flags_a_written_cache_and_exits_nonzero(tmp_path: Path, capsys) -> None:
    bad = _write(
        tmp_path, "bad.py", "_cache = {}\n\n\ndef put(k, v):\n    _cache[k] = v\n"
    )
    assert mod.main([str(bad)]) == 1
    err = capsys.readouterr().err
    assert f"{bad}:1:" in err
    assert mod.DEFAULT_RESET_NAME in err
    assert "allow-unreset-state" in err


def test_main_clean_file_exits_zero(tmp_path: Path) -> None:
    good = _write(
        tmp_path, "good.py", "_conf = {'a': 1}\n\n\ndef get(k):\n    return _conf[k]\n"
    )
    assert mod.main([str(good)]) == 0


def test_main_honours_a_custom_reset_name_flag(tmp_path: Path, capsys) -> None:
    src = (
        "_cache = {}\n\n\ndef put(k, v):\n    _cache[k] = v\n\n\n"
        "def _teardown() -> None:\n    _cache.clear()\n"
    )
    path = _write(tmp_path, "custom.py", src)
    assert mod.main(["--reset-name", "_teardown", str(path)]) == 0
    assert mod.main([str(path)]) == 1  # the default reset name still flags it
    del capsys


def test_main_with_only_a_flag_and_no_files_exits_two(capsys) -> None:
    assert mod.main(["--reset-name", "_teardown"]) == 2
    assert "no files to scan" in capsys.readouterr().err


def test_main_with_no_args_at_all_exits_two_via_run_file_cli() -> None:
    # Wired through `run_file_cli` at `__main__` — only observable by running
    # the module, since calling `mod.main` directly always receives an argv.
    done = subprocess.run(
        [sys.executable, "-m", "ci_truth_serum.check_unreset_module_state"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 2


# --------------------------------------------------------------------------- #
# Non-vacuity: renaming the reset in an otherwise-clean fixture brings a
# violation back — proves the "clean because it resets" path is not vacuous.
# --------------------------------------------------------------------------- #
def test_removing_the_reset_brings_a_violation_back() -> None:
    text = (
        "_cache: dict = {}\n"
        "\n"
        "\n"
        "def remember(key, value):\n"
        "    _cache[key] = value\n"
        "\n"
        "\n"
        "def _reset_process_state() -> None:\n"
        "    _cache.clear()\n"
    )
    assert mod.violations(text) == []
    stripped = text.replace(f"def {mod.DEFAULT_RESET_NAME}", "def _not_a_reset")
    assert stripped != text
    assert mod.declares_reset(ast.parse(stripped)) is False
    assert mod.violations(stripped) == [1]
