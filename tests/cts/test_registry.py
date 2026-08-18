"""Tests for ci_truth_serum/_registry.py — the metadata SSOT that says which
checks this pack ships, what tier each sits in, and what each one is about.

The contract tests pin the registry to the two surfaces that must agree with it:
`.pre-commit-hooks.yaml` (every tagged check has a hook id, and its primary tag
agrees with the category its `name:` prefix declares) and the README (every tag
in the vocabulary is documented). A tag nobody documents is a tag nobody selects.
"""

import yaml

from tests._helpers import REPO_ROOT, load_hook

reg = load_hook("_registry.py", "_registry")

MANIFEST = yaml.safe_load((REPO_ROOT / ".pre-commit-hooks.yaml").read_text())
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

# The category a hook's `name:` prefix declares, mapped to the tag that says the
# same thing. `opinionated` and `extra` name a tier, not a topic, so they are
# absent: those hooks carry whatever topic tags apply.
PREFIX_TAG = {
    "honesty": "honesty",
    "identity": "supply-chain",
    "security": "security",
}


def test_every_check_carries_at_least_one_tag():
    untagged = [c.module for c in reg.CHECKS if not c.tags]
    assert untagged == []


def test_every_tag_is_in_the_closed_vocabulary():
    unknown = {t for c in reg.CHECKS for t in c.tags} - reg.TAGS
    assert unknown == set()


def test_every_vocabulary_tag_has_a_member():
    """A tag with no check is a selector that resolves to an empty run."""
    unused = reg.TAGS - {t for c in reg.CHECKS for t in c.tags}
    assert unused == set()


def test_modules_are_unique():
    modules = [c.module for c in reg.CHECKS]
    assert len(modules) == len(set(modules))


def test_hook_id_is_the_module_in_kebab_case():
    check = next(c for c in reg.CHECKS if c.module == "check_pinned_base_images")
    assert check.hook_id == "check-pinned-base-images"


def test_every_registered_check_has_a_hook_in_the_manifest():
    ids = {h["id"] for h in MANIFEST}
    missing = [c.hook_id for c in reg.CHECKS if c.hook_id not in ids]
    assert missing == []


def test_the_name_prefix_and_the_tag_agree():
    """The manifest states each check's category in prose, in its `name:`. The
    registry states it as a tag. A check whose two statements disagree is the
    drift this test exists to catch."""
    by_id = {c.hook_id: c for c in reg.CHECKS}
    checked = 0
    for hook in MANIFEST:
        check = by_id.get(hook["id"])
        prefix = hook["name"].split(":", 1)[0]
        if check is None or prefix not in PREFIX_TAG:
            continue
        assert PREFIX_TAG[prefix] in check.tags, hook["id"]
        checked += 1
    # Non-vacuity: those three prefixes are exactly the Tier 1 hooks, so the
    # loop must have judged every one of them.
    assert checked == len(reg.TIERS["1"])


def test_tiers_partition_the_registry():
    assert sum(len(v) for v in reg.TIERS.values()) == len(reg.CHECKS)
    assert set(reg.TIERS) == {"1", "2", "extras"}


def test_tier_members_keep_their_registry_kind():
    kinds = {(c.module, c.kind) for c in reg.CHECKS}
    assert {m for members in reg.TIERS.values() for m in members} == kinds


def test_by_tag_returns_the_carriers_only():
    security = {c.module for c in reg.by_tag("security")}
    assert "check_trusted_base" in security
    assert "check_doc_line_refs" not in security


def test_a_check_can_carry_several_tags():
    """Tags are a multi-label axis; a single-label one would force
    check_token_fallback to choose between secrets and security."""
    token = next(c for c in reg.CHECKS if c.module == "check_token_fallback")
    assert {"secrets", "security"} <= token.tags


def test_tag_index_lists_hook_ids_per_tag():
    index = reg.tag_index()
    assert set(index) == set(reg.TAGS)
    assert "check-cron-alert-coverage" in index["alerting"]


def test_the_readme_documents_every_tag():
    undocumented = [t for t in sorted(reg.TAGS) if f"`{t}`" not in README]
    assert undocumented == []
