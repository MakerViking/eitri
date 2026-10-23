# SPDX-License-Identifier: Apache-2.0
"""version_key: ordering of CalVer releases, pre-releases, and non-CalVer installed markers."""
from eitri_update.manifest import version_key


def test_calver_orders_numerically():
    assert version_key("2026.10.2") < version_key("2026.10.10")
    assert version_key("2026.02.3") < version_key("2026.10.0")
    assert version_key("2025.12.9") < version_key("2026.01.0")


def test_prerelease_sorts_before_its_release():
    # a -dev/-rc pre-release of a version comes before the final release
    assert version_key("2026.10.23-dev.b2afdcb") < version_key("2026.10.23")
    # ...but after the previous release
    assert version_key("2026.10.22") < version_key("2026.10.23-dev.b2afdcb")


def test_non_calver_installed_marker_does_not_crash_and_sorts_first():
    # a hand-flashed bench build leaves `git describe` like "6888c58-dirty";
    # it must not raise (int('6888c58') would) and must sort before any release,
    # so an upgrade onto it is always offered.
    k = version_key("6888c58-dirty")
    assert k == (-1,)
    assert k < version_key("2026.02.3")
    assert k < version_key("2026.10.23-dev.b2afdcb")


def test_plain_unknown_strings_are_safe():
    for s in ("", "unknown", "dirty", "v2026.10", "0.0.0.dev0"):
        assert version_key(s) < version_key("2026.02.3")
