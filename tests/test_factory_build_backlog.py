#!/usr/bin/env python3
"""Cover the factory build backlog catalog and its auditor.

The catalog (``config/factory-build-backlog.toml``) is the surface that
makes the 551-name backlog from projectbluefin/utah-packages#308
shrinkable: every name must appear in exactly one area, the area totals
must reconcile with the audit's 551, and removing a name requires it to
also land in ``[resolved]`` or ``[wontfix]`` so the count never silently
shrinks. The auditor (``tools/factory_build_backlog.py``) partitions
catalog names against the live repo state (``packages/``,
``config/upstream-sources.json``, ``.packit.yaml``, and
``config/bluefin-packages.toml``) so every import lands in
``already_*`` and reduces the ``pending`` total.

Tests construct a minimal tree on disk so the auditor and catalog can be
exercised without pulling or modifying the live repository.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from tools import factory_build_backlog as backlog


def _toml(catalog_root: Path, areas: dict[str, list[str]], resolved: list[str] | None = None, wontfix: list[str] | None = None) -> Path:
    """Write a minimal ``config/factory-build-backlog.toml`` and return its path."""
    resolved = resolved or []
    wontfix = wontfix or []
    config_dir = catalog_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "factory-build-backlog.toml"
    lines = [
        "[meta]",
        'issue = "projectbluefin/utah-packages#308"',
        'audit_source = "https://example.test/gist"',
        'audit_digest_bluefin = "sha256:00"',
        'audit_digest_utah = "sha256:00"',
        'audit_digest_factory = "sha256:00"',
        'audit_measured_at = "2026-09-30T22:08:14Z"',
        "",
    ]
    for area, names in areas.items():
        lines.append(f"[areas.{area}]")
        lines.append('consumer_issue = "projectbluefin/utah#1"')
        lines.append("packages = [")
        for name in names:
            lines.append(f'    "{name}",')
        lines.append("]")
        lines.append("")
    lines.append("[resolved]")
    lines.append("packages = [")
    for entry in resolved:
        if isinstance(entry, dict):
            name = entry["name"]
            commit = entry.get("commit", "0" * 40)
            lines.append(f'    {{ name = "{name}", commit = "{commit}" }},')
        else:
            lines.append(f'    {{ name = "{entry}", commit = "{"0" * 40}" }},')
    lines.append("]")
    lines.append("")
    lines.append("[wontfix]")
    lines.append("packages = [")
    for entry in wontfix:
        if isinstance(entry, dict):
            name = entry["name"]
            reason = entry.get("reason", "wontfix")
            lines.append(f'    {{ name = "{name}", reason = "{reason}" }},')
        else:
            lines.append(f'    {{ name = "{entry}", reason = "wontfix" }},')
    lines.append("]")
    lines.append("")
    path.write_text("\n".join(lines))
    return path


def _populate(root: Path, *, recipes: list[str], locked: list[str], packit: list[str], manifest: list[str]) -> None:
    """Materialize the artifacts the auditor reads against."""
    packages_dir = root / "packages"
    packages_dir.mkdir(exist_ok=True)
    for name in recipes:
        (packages_dir / name).mkdir()
        (packages_dir / name / "sources").write_text("")
        # A spec file is what the live ``package_inventory`` expects; without
        # one the inventory raises and the auditor's snapshot-check guard
        # breaks for the wrong reason.
        (packages_dir / name / f"{name}.spec").write_text(f"Name: {name}\nVersion: 0\n")
        (packages_dir / name / ".hummingbird-upstream.json").write_text(
            json.dumps(
                {
                    "package": name,
                    "branch": "rawhide",
                    "remote": "https://example.test/rpms/" + name,
                    "commit": "0" * 40,
                    "tree": "0" * 40,
                    "imported_at": "2026-09-30T22:00:00Z",
                }
            )
        )
    (root / "config").mkdir(exist_ok=True)
    locks_data = {
        "schema": 1,
        "packages": [
            {"name": n, "url": f"https://example.test/{n}.tar.gz", "filename": f"{n}.tar.gz", "sha512": "0" * 128}
            for n in locked
        ],
    }
    (root / "config" / "upstream-sources.json").write_text(json.dumps(locks_data))
    packit_lines = ["packages:\n"]
    for name in packit:
        packit_lines.append(f"  {name}:\n    specfile_path: {name}.spec\n")
    (root / ".packit.yaml").write_text("".join(packit_lines))
    manifest_lines = ["[base]\n", 'packages = [']
    for name in manifest:
        manifest_lines.append(f'    "{name}",')
    manifest_lines.append("]\n")
    (root / "config" / "bluefin-packages.toml").write_text("".join(manifest_lines))


class CatalogTotalsTests(unittest.TestCase):
    def test_collects_one_set_per_area(self) -> None:
        catalog = tomllib.loads(
            '[areas.a]\npackages = ["x", "y"]\n'
            '[areas.b]\npackages = ["z"]\n'
            '[resolved]\npackages = []\n'
            '[wontfix]\npackages = []\n'
        )
        all_backlog, by_area, wontfix, resolved = backlog._catalog_totals(catalog)
        self.assertEqual(all_backlog, {"x", "y", "z"})
        self.assertEqual(by_area, {"a": {"x", "y"}, "b": {"z"}})
        self.assertEqual(wontfix, set())
        self.assertEqual(resolved, set())


class ClassifyTests(unittest.TestCase):
    """The partition order is significant: the strongest signal wins."""

    def test_recipe_wins_over_lock(self) -> None:
        state = backlog._classify("fish", recipes={"fish"}, subpackages=set(), locks={"fish"}, packit={"fish"}, manifest={"fish"})
        self.assertEqual(state, "already_recipe")

    def test_lock_wins_over_packit(self) -> None:
        state = backlog._classify("fish", recipes=set(), subpackages=set(), locks={"fish"}, packit={"fish"}, manifest={"fish"})
        self.assertEqual(state, "already_locked")

    def test_packit_wins_over_manifest(self) -> None:
        state = backlog._classify("fish", recipes=set(), subpackages=set(), locks=set(), packit={"fish"}, manifest={"fish"})
        self.assertEqual(state, "already_packit")

    def test_manifest_wins_over_pending(self) -> None:
        state = backlog._classify("fish", recipes=set(), subpackages=set(), locks=set(), packit=set(), manifest={"fish"})
        self.assertEqual(state, "manifest_wants")

    def test_pending_when_no_signal(self) -> None:
        state = backlog._classify("fish", recipes=set(), subpackages=set(), locks=set(), packit=set(), manifest=set())
        self.assertEqual(state, "pending")

    def test_subpackage_wins_over_everything(self) -> None:
        # A name that is a ``%package -n`` subpackage of an existing recipe
        # is built by the factory even when no other signal is present --
        # a fresh spec with only ``%package -n libavcodec`` lines (no
        # packit, no lock, no manifest) still resolves to ``already_recipe``.
        state = backlog._classify("libavcodec", recipes=set(), subpackages={"libavcodec"}, locks=set(), packit=set(), manifest=set())
        self.assertEqual(state, "already_recipe")

    def test_subpackage_does_not_mask_recipe_partition(self) -> None:
        # If a name happens to match both ``recipes`` and ``subpackages``,
        # it still resolves to ``already_recipe`` (the same state). The
        # ``subpackages`` set only matters when ``recipes`` is empty --
        # which is the whole point of classifying subpackage names
        # without false-positiving on names that already have their own
        # recipe directory.
        state = backlog._classify("fish", recipes={"fish"}, subpackages={"fish"}, locks=set(), packit=set(), manifest=set())
        self.assertEqual(state, "already_recipe")


class PackitAndLockTests(unittest.TestCase):
    def test_packit_names_returns_every_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".packit.yaml").write_text(
                "packages:\n"
                "  fish:\n    specfile_path: fish.spec\n"
                "  zsh:\n    specfile_path: zsh.spec\n"
            )
            self.assertEqual(backlog._packit_names(root / ".packit.yaml"), {"fish", "zsh"})

    def test_packit_names_handles_plus_and_dot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".packit.yaml").write_text(
                "packages:\n"
                "  python3-keyring+completion:\n    specfile_path: a.spec\n"
                "  vid.stab:\n    specfile_path: b.spec\n"
            )
            self.assertEqual(backlog._packit_names(root / ".packit.yaml"), {"python3-keyring+completion", "vid.stab"})

    def test_lock_names_reads_schema_1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "upstream-sources.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "packages": [
                            {"name": "fish"},
                            {"name": "zsh"},
                        ],
                    }
                )
            )
            self.assertEqual(backlog._lock_names(root / "upstream-sources.json"), {"fish", "zsh"})

    def test_manifest_names_collects_every_section(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bluefin-packages.toml").write_text(
                "[base]\npackages = [\"a\"]\n"
                "[fedora_v44]\npackages = [\"b\", \"c\"]\n"
                "[excluded]\npackages = [\"d\"]\n"
            )
            self.assertEqual(backlog._manifest_names(root / "bluefin-packages.toml"), {"a", "b", "c"})


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="backlog-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _write(self) -> Path:
        catalog_path = _toml(
            self.root,
            areas={"codecs-media": ["ffmpeg", "x264-libs"], "power": ["tuned"]},
        )
        _populate(
            self.root,
            recipes=["ffmpeg"],
            locked=[],
            packit=[],
            manifest=[],
        )
        return catalog_path

    def test_partition_count_sums_match_catalog(self) -> None:
        report = backlog._report(self.root, self._write())
        states = report["states"]
        total = sum(states.values())
        self.assertEqual(total, report["totals"]["backlog"])
        self.assertEqual(report["totals"]["backlog"], 3)
        self.assertEqual(states["already_recipe"], 1)
        self.assertEqual(states["pending"], 2)

    def test_categorization_resolves_a_recipe_to_already_recipe(self) -> None:
        report = backlog._report(self.root, self._write())
        ffmpeg = next(entry for entry in report["entries"] if entry["name"] == "ffmpeg")
        self.assertEqual(ffmpeg["state"], "already_recipe")

    def test_pending_count_rolls_up_per_area(self) -> None:
        report = backlog._report(self.root, self._write())
        self.assertEqual(report["areas"]["codecs-media"]["pending"], 1)
        self.assertEqual(report["areas"]["codecs-media"]["already_recipe"], 1)
        self.assertEqual(report["areas"]["power"]["pending"], 1)

    def test_resolved_entries_drop_pending(self) -> None:
        # ``tuned`` is removed from the area and recorded in ``[resolved]``;
        # this is the documented contract for closing a backlog gap.
        catalog_path = _toml(
            self.root,
            areas={"power": []},
            resolved=[{"name": "tuned", "commit": "0" * 40}],
        )
        _populate(self.root, recipes=[], locked=[], packit=[], manifest=[])
        report = backlog._report(self.root, catalog_path)
        self.assertEqual(report["totals"]["backlog"], 0)
        self.assertEqual(report["totals"]["resolved"], 1)

    def test_wontfix_entries_count_as_resolved(self) -> None:
        # Same contract: dropping a name from the area must record the
        # decision in ``[wontfix]`` so the count never silently shrinks.
        catalog_path = _toml(
            self.root,
            areas={"power": []},
            wontfix=[{"name": "tuned", "reason": "wontfix"}],
        )
        _populate(self.root, recipes=[], locked=[], packit=[], manifest=[])
        report = backlog._report(self.root, catalog_path)
        self.assertEqual(report["totals"]["wontfix"], 1)
        self.assertEqual(report["totals"]["backlog"], 0)
        self.assertEqual(report["entries"], [])


class CatalogConsistencyTests(unittest.TestCase):
    """The catalog itself is a contract: 551 total, no duplicates across areas.

    The 551-name audit source is fixed; closing a gap moves a name out of
    an area into either ``[resolved]`` or ``[wontfix]`` (the two-edit
    closing contract documented in docs/skills/factory-build-backlog.md).
    That means the assertion is on the *sum* of the three tables, not on
    the backlog alone -- a PR that closes a gap without recording the
    decision would shrink the sum below 551, and a PR that closes a gap
    with a one-edit (drop only) would shrink the sum below 551 too.
    """

    CATALOG_TOTAL = 551

    def test_real_catalog_matches_the_audit_count(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        with (repo_root / "config" / "factory-build-backlog.toml").open("rb") as handle:
            catalog = tomllib.load(handle)
        all_backlog, by_area, wontfix, resolved = backlog._catalog_totals(catalog)
        self.assertEqual(
            len(all_backlog) + len(resolved) + len(wontfix),
            self.CATALOG_TOTAL,
            f"factory-build-backlog.toml must record all {self.CATALOG_TOTAL} names "
            "(sum of areas + [resolved] + [wontfix]) from issue #308",
        )
        # Every name appears in exactly one area; duplicates would inflate the count above the area total.
        per_name = {}
        for area, names in by_area.items():
            for name in names:
                per_name[name] = per_name.get(name, 0) + 1
        duplicates = sorted(name for name, count in per_name.items() if count != 1)
        self.assertEqual(duplicates, [], f"catalog lists names in more than one area: {duplicates}")
        # Resolved and wontfix entries must not appear in any area: the catalog
        # design says once a name leaves the backlog it must land in one of
        # these two tables, and the next import must move it.
        removed = wontfix | resolved
        overlap = removed & all_backlog
        self.assertEqual(overlap, set(), f"these names are in both the backlog and a resolved table: {sorted(overlap)}")
        # A name may not be both resolved and wontfixed: the decision is one
        # of two, never both.
        both = resolved & wontfix
        self.assertEqual(both, set(), f"catalog lists names in both [resolved] and [wontfix]: {sorted(both)}")

    def test_real_report_matches_the_catalog_total(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        report = backlog._report(repo_root, repo_root / "config" / "factory-build-backlog.toml")
        # The report's totals are a partition of the catalog: backlog +
        # resolved + wontfix must equal the audit count. ``states`` only
        # covers backlog entries (every entry is classified once), so
        # ``sum(states) == backlog`` -- that's the correct shape, not a
        # regression against the audit count.
        totals = report["totals"]
        self.assertEqual(
            totals["backlog"] + totals["resolved"] + totals["wontfix"],
            self.CATALOG_TOTAL,
        )
        states_total = sum(report["states"].values())
        self.assertEqual(states_total, totals["backlog"])


class CatalogOverlapTests(unittest.TestCase):
    """The auditor must surface every catalog contract break, not silently pass."""

    def test_overlap_area_and_resolved_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = _toml(
                root,
                areas={"power": ["tuned", "thermald"]},
                resolved=[{"name": "tuned", "commit": "0" * 40}],
            )
            _populate(root, recipes=[], locked=[], packit=[], manifest=[])
            with self.assertRaises(SystemExit) as caught:
                backlog._report(root, catalog_path)
            self.assertIn("tuned", str(caught.exception))
            self.assertIn("area and [resolved]", str(caught.exception))

    def test_overlap_area_and_wontfix_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = _toml(
                root,
                areas={"power": ["tuned"]},
                wontfix=[{"name": "tuned", "reason": "wontfix"}],
            )
            _populate(root, recipes=[], locked=[], packit=[], manifest=[])
            with self.assertRaises(SystemExit) as caught:
                backlog._report(root, catalog_path)
            self.assertIn("tuned", str(caught.exception))
            self.assertIn("area and [wontfix]", str(caught.exception))

    def test_overlap_resolved_and_wontfix_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = _toml(
                root,
                areas={"power": []},
                resolved=[{"name": "tuned", "commit": "0" * 40}],
                wontfix=[{"name": "tuned", "reason": "wontfix"}],
            )
            _populate(root, recipes=[], locked=[], packit=[], manifest=[])
            with self.assertRaises(SystemExit) as caught:
                backlog._report(root, catalog_path)
            self.assertIn("tuned", str(caught.exception))
            self.assertIn("[resolved] and [wontfix]", str(caught.exception))

    def test_duplicate_across_areas_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = _toml(
                root,
                areas={"power": ["tuned"], "codecs-media": ["tuned"]},
            )
            _populate(root, recipes=[], locked=[], packit=[], manifest=[])
            with self.assertRaises(SystemExit) as caught:
                backlog._report(root, catalog_path)
            self.assertIn("more than one area", str(caught.exception))


class CheckGateTests(unittest.TestCase):
    def test_check_passes_on_a_fresh_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = _toml(
                root,
                areas={"power": ["tuned"], "codecs-media": ["ffmpeg"]},
            )
            _populate(root, recipes=["ffmpeg"], locked=[], packit=[], manifest=[])
            report = backlog._report(root, catalog_path)
            # Write the snapshot first so --check has a target.
            snapshot = root / "reports" / "factory-build-backlog.json"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            # Re-run the report generation and persist a comparable snapshot.
            snapshot.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
            self.assertEqual(backlog._check(report, snapshot), 0)

    def test_check_fails_on_stale_totals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = _toml(root, areas={"power": ["tuned"]})
            _populate(root, recipes=[], locked=[], packit=[], manifest=[])
            report = backlog._report(root, catalog_path)
            snapshot = root / "reports" / "factory-build-backlog.json"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            # Write a snapshot whose ``totals`` differ from ``report`` so the
            # check must surface the drift.
            stale = json.loads(json.dumps(report))
            stale["totals"]["backlog"] = 999
            snapshot.write_text(json.dumps(stale, indent=2, sort_keys=True) + "\n")
            self.assertEqual(backlog._check(report, snapshot), 1)

    def test_check_fails_on_missing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = _toml(root, areas={"power": ["tuned"]})
            _populate(root, recipes=[], locked=[], packit=[], manifest=[])
            report = backlog._report(root, catalog_path)
            snapshot = root / "reports" / "factory-build-backlog.json"
            self.assertEqual(backlog._check(report, snapshot), 1)

    def test_check_fails_when_a_name_moved_between_areas(self) -> None:
        """Totals and states are blind to a move; areas and entries are not."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = _toml(
                root,
                areas={"power": ["tuned"], "codecs-media": ["ffmpeg"]},
            )
            _populate(root, recipes=[], locked=[], packit=[], manifest=[])
            report = backlog._report(root, catalog_path)
            stale = json.loads(json.dumps(report))
            for entry in stale["entries"]:
                entry["area"] = "power" if entry["area"] == "codecs-media" else "codecs-media"
            stale["areas"] = {
                "power": report["areas"]["codecs-media"],
                "codecs-media": report["areas"]["power"],
            }
            self.assertEqual(stale["totals"], report["totals"])
            self.assertEqual(stale["states"], report["states"])
            snapshot = root / "reports" / "factory-build-backlog.json"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(json.dumps(stale, indent=2, sort_keys=True) + "\n")
            self.assertEqual(backlog._check(report, snapshot), 1)

    def test_report_has_no_wall_clock_field(self) -> None:
        """Two runs of the same tree must produce identical bytes."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = _toml(root, areas={"power": ["tuned"]})
            _populate(root, recipes=[], locked=[], packit=[], manifest=[])
            first = backlog._report(root, catalog_path)
            second = backlog._report(root, catalog_path)
            self.assertNotIn("measured_at", first)
            self.assertEqual(
                json.dumps(first, indent=2, sort_keys=True),
                json.dumps(second, indent=2, sort_keys=True),
            )

    def test_check_resolves_paths_against_root_not_cwd(self) -> None:
        """``--root <tree> --check`` must work from any working directory."""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as elsewhere:
            root = Path(directory)
            _toml(root, areas={"power": ["tuned"]})
            _populate(root, recipes=[], locked=[], packit=[], manifest=[])
            self.assertEqual(backlog.main(["--root", str(root)]), 0)
            cwd = os.getcwd()
            os.chdir(elsewhere)
            try:
                self.assertEqual(backlog.main(["--root", str(root), "--check"]), 0)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
