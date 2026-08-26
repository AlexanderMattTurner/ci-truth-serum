"""Tests for ci_truth_serum/check_relative_imports.py — the lint that requires every
static relative import/export specifier to resolve to an existing FILE, matching
Node's ESM resolver (no extension guessing, no directory-index fallback).

Drives ``relative_specifiers()`` for the extraction rules, ``violations()`` for
resolution against the real filesystem, and ``main()`` for the argv/exit-code
contract.
"""

import subprocess
import sys

from tests._helpers import REPO_ROOT, load_hook

mod = load_hook("check_relative_imports.py", "check_relative_imports")


def _specifiers(src: str) -> list[str]:
    return [specifier for _, specifier in mod.relative_specifiers(src, "f.mjs")]


# ── extraction: every syntax form, and nothing that merely looks like one ──
def test_every_static_import_export_form_is_extracted() -> None:
    src = "\n".join(
        [
            'import a from "./a.mjs";',
            'import { b } from "./sub/b.mjs";',
            'import "./side-effect.mjs";',
            'import * as c from "../c.mjs";',
            'export { d } from "./d.mjs";',
            'export * from "./e.mjs";',
            'const f = await import("./f.mjs");',
        ]
    )
    assert _specifiers(src) == [
        "./a.mjs",
        "./sub/b.mjs",
        "./side-effect.mjs",
        "../c.mjs",
        "./d.mjs",
        "./e.mjs",
        "./f.mjs",
    ]


def test_bare_and_subpath_specifiers_are_not_relative() -> None:
    src = "\n".join(
        [
            'import { readFileSync } from "node:fs";',
            'import ts from "typescript";',
            'import y from "#internal";',
            'import z from "./real.mjs";',
        ]
    )
    assert _specifiers(src) == ["./real.mjs"]


def test_a_relative_looking_path_in_a_string_or_comment_is_not_an_import() -> None:
    src = "\n".join(
        [
            '// import x from "./commented-out.mjs";',
            '/* import y from "./block-comment.mjs"; */',
            'const path = "./data.mjs";',
            "const tpl = `./template.mjs`;",
            'import real from "./real.mjs";',
        ]
    )
    assert _specifiers(src) == ["./real.mjs"]


def test_a_no_substitution_template_specifier_counts() -> None:
    # TypeScript's own resolver reads a plain template literal as a specifier.
    src = "const g = await import(`./g.mjs`);"
    assert _specifiers(src) == ["./g.mjs"]


def test_a_computed_dynamic_import_is_skipped() -> None:
    src = "\n".join(
        [
            "const mod = await import(`./gen/${name}.mjs`);",
            "const other = await import(chosenPath);",
            'const literal = await import("./known.mjs");',
        ]
    )
    assert _specifiers(src) == ["./known.mjs"]


def test_an_escaped_specifier_is_skipped_rather_than_decoded() -> None:
    src = 'import a from "./a\\u002e.mjs";'
    assert _specifiers(src) == []


def test_the_reported_line_is_the_specifiers_own_line() -> None:
    src = '\n\n\nimport a from "./a.mjs";\n\n\nimport b from "./b.mjs";'
    assert mod.relative_specifiers(src, "f.mjs") == [
        (4, "./a.mjs"),
        (7, "./b.mjs"),
    ]


# ── resolution against the real filesystem ─────────────────────────────────
def _write(tmp_path, rel: str, contents: str = "export const x = 1;\n"):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    return path


def test_a_specifier_naming_an_existing_file_resolves_clean(tmp_path) -> None:
    _write(tmp_path, "pkg/inner.mjs")
    _write(tmp_path, "sibling.mjs")
    entry = _write(
        tmp_path,
        "pkg/entry.mjs",
        'import { y } from "./inner.mjs";\nimport { x } from "../sibling.mjs";\n',
    )
    assert mod.violations(entry.read_text(), str(entry)) == []


def test_a_specifier_naming_no_file_is_reported(tmp_path) -> None:
    entry = _write(tmp_path, "pkg/entry.mjs", 'import { z } from "./missing.mjs";\n')
    assert mod.violations(entry.read_text(), str(entry)) == [1]


def test_an_extensionless_specifier_is_reported() -> None:
    # Node ESM guesses no extension — the exact shape a require-era habit leaves.
    entry = "pkg/entry.mjs"
    src = 'import { y } from "./inner";\n'
    assert mod.violations(src, entry) == [1]


def test_a_specifier_naming_a_directory_is_reported(tmp_path) -> None:
    # Node has no directory-index fallback: ERR_UNSUPPORTED_DIR_IMPORT.
    (tmp_path / "pkg" / "nested").mkdir(parents=True)
    entry = _write(tmp_path, "pkg/entry.mjs", 'import x from "./nested";\n')
    assert mod.violations(entry.read_text(), str(entry)) == [1]


def test_a_cache_busting_query_or_fragment_is_stripped_before_resolution(
    tmp_path,
) -> None:
    _write(tmp_path, "pkg/inner.mjs")
    entry = _write(
        tmp_path,
        "pkg/entry.mjs",
        'import { y } from "./inner.mjs?v=2";\nimport { z } from "./inner.mjs#frag";\n',
    )
    assert mod.violations(entry.read_text(), str(entry)) == []
    # …and stripping must not make a genuinely missing file look present.
    bad = _write(tmp_path, "pkg/other.mjs", 'import z from "./gone.mjs?v=2";\n')
    assert mod.violations(bad.read_text(), str(bad)) == [1]


def test_the_wrong_depth_dotdot_is_caught(tmp_path) -> None:
    # A file moved one directory deeper kept a `../` that is now one level short.
    _write(tmp_path, "sibling.mjs")
    entry = _write(
        tmp_path,
        "a/b/entry.mjs",
        'import x from "../sibling.mjs";\n',
    )
    assert mod.violations(entry.read_text(), str(entry)) == [1]


def test_a_non_js_path_has_no_specifiers_to_check(tmp_path) -> None:
    entry = _write(tmp_path, "notes.txt", 'import x from "./missing.mjs";\n')
    assert mod.violations(entry.read_text(), str(entry)) == []


# ── opt-out ─────────────────────────────────────────────────────────────────
def test_opt_out_on_the_import_line() -> None:
    src = 'import z from "./missing.mjs"; // allow-dangling-import: built later\n'
    assert mod.violations(src, "pkg/entry.mjs") == []


def test_opt_out_on_the_line_above() -> None:
    src = '// allow-dangling-import: built later\nimport z from "./missing.mjs";\n'
    assert mod.violations(src, "pkg/entry.mjs") == []


def test_an_opt_out_without_a_reason_does_not_exempt() -> None:
    src = 'import z from "./missing.mjs"; // allow-dangling-import:\n'
    assert mod.violations(src, "pkg/entry.mjs") == [1]


# ── non-vacuity ──────────────────────────────────────────────────────────────
def test_two_missing_specifiers_are_both_flagged() -> None:
    src = 'import a from "./one.mjs";\nimport b from "./two.mjs";\n'
    assert mod.violations(src, "pkg/entry.mjs") == [1, 2]


# ── main: argv/exit-code contract ───────────────────────────────────────────
def test_main_reports_and_exits_nonzero(tmp_path, capsys) -> None:
    entry = _write(tmp_path, "pkg/entry.mjs", 'import z from "./missing.mjs";\n')
    assert mod.main([str(entry)]) == 1
    assert f"{entry}:1:" in capsys.readouterr().err


def test_main_clean_file_exits_zero(tmp_path) -> None:
    _write(tmp_path, "pkg/inner.mjs")
    entry = _write(tmp_path, "pkg/entry.mjs", 'import x from "./inner.mjs";\n')
    assert mod.main([str(entry)]) == 0


def test_empty_argv_exits_2_via_run_file_cli() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "ci_truth_serum.check_relative_imports"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "no files to scan" in proc.stderr
