"""Tests for hooks/check_secret_file_perms.py — the pre-commit lint that flags a
secret file created world-readable and only chmod'd private on a later line.

Drives `violations()` directly so each rule is asserted in isolation, pinning the
EXACT create line numbers.
"""

from pathlib import Path

import pytest

from tests._helpers import load_hook

mod = load_hook("check_secret_file_perms.py", "check_secret_file_perms")


def test_create_then_chmod_fires_at_the_create_line() -> None:
    src = (
        "#!/usr/bin/env bash\n"  # 1
        "printf '%s' \"$TOKEN\" > tokenfile\n"  # 2  <- create
        "chmod 600 tokenfile\n"  # 3
    )
    assert mod.violations(src) == [2]


@pytest.mark.parametrize(
    "name, src, line",
    [
        (
            "redirect create then later chmod",
            "#!/usr/bin/env bash\n"
            "echo secret > /run/creds.json\n"
            'log "wrote it"\n'
            "chmod 0400 /run/creds.json\n",
            2,
        ),
        (
            "touch create then chmod",
            "#!/usr/bin/env bash\ntouch ~/.npmrc\nchmod 600 ~/.npmrc\n",
            2,
        ),
        (
            "tee create then chmod",
            '#!/usr/bin/env bash\necho "$K" | tee id_rsa\nchmod 0600 id_rsa\n',
            2,
        ),
        (
            "non-private install then chmod",
            "#!/usr/bin/env bash\n"
            "install auth.pem /etc/auth.pem\n"
            "chmod 400 /etc/auth.pem\n",
            2,
        ),
        (
            "one-liner create && chmod on same line",
            "#!/usr/bin/env bash\nprintf x > passwdfile && chmod 600 passwdfile\n",
            2,
        ),
    ],
)
def test_unguarded_secret_creates_are_flagged(name: str, src: str, line: int) -> None:
    assert mod.violations(src) == [line], name


# Every guarded / benign create => zero findings.
@pytest.mark.parametrize(
    "name, src",
    [
        (
            "inline subshell umask on the create line",
            "#!/usr/bin/env bash\n"
            "(umask 077; printf '%s' \"$T\" > tokenfile)\n"
            "chmod 600 tokenfile\n",
        ),
        (
            "standing umask earlier in the file",
            "#!/usr/bin/env bash\n"
            "umask 077\n"
            "printf '%s' \"$T\" > tokenfile\n"
            "chmod 600 tokenfile\n",
        ),
        (
            "install -m 600 is already private",
            "#!/usr/bin/env bash\n"
            "install -m 600 credfile /etc/credfile\n"
            "chmod 600 /etc/credfile\n",
        ),
        (
            "create with no nearby chmod",
            "#!/usr/bin/env bash\nprintf '%s' \"$T\" > tokenfile\necho done\n",
        ),
        (
            "chmod too far away (>3 non-blank lines)",
            "#!/usr/bin/env bash\n"
            "printf x > tokenfile\n"
            "a=1\n"
            "b=2\n"
            "c=3\n"
            "chmod 600 tokenfile\n",
        ),
        (
            "non-secret-named path, create then chmod",
            "#!/usr/bin/env bash\necho hi > output.txt\nchmod 600 output.txt\n",
        ),
        (
            "chmod is not private (0644)",
            "#!/usr/bin/env bash\nprintf x > tokenfile\nchmod 644 tokenfile\n",
        ),
    ],
)
def test_guarded_or_benign_creates_pass(name: str, src: str) -> None:
    assert mod.violations(src) == [], name


def test_secret_perms_ok_marker_suppresses() -> None:
    src = (
        "#!/usr/bin/env bash\n"
        "printf x > tokenfile  # secret-perms-ok: created empty, populated privately below\n"
        "chmod 600 tokenfile\n"
    )
    assert mod.violations(src) == []


def test_main_wires_violations_and_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() runs this script's detector through the shared loop with its own
    message; the generic loop behaviour is covered in test_linecheck.py."""
    bad = tmp_path / "bad.sh"
    bad.write_text(
        "#!/usr/bin/env bash\nprintf x > tokenfile\nchmod 600 tokenfile\n",
        encoding="utf-8",
    )
    assert mod.main([str(bad)]) == 1
    assert f"{bad}:2: creates a secret file world-readable" in capsys.readouterr().err


def test_main_passes_clean_file(tmp_path: Path) -> None:
    good = tmp_path / "good.sh"
    good.write_text(
        "#!/usr/bin/env bash\n(umask 077; printf x > tokenfile)\nchmod 600 tokenfile\n",
        encoding="utf-8",
    )
    assert mod.main([str(good)]) == 0
