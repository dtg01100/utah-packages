#!/usr/bin/env python3
"""Measure the factory build backlog against the current repository state.

Issue ``projectbluefin/utah-packages#308`` records the 551 Bluefin names that
Utah's bare-metal audit (2026-09-30) found in neither the pinned factory repo
nor the Hummingbird supply. Each entry is a candidate factory build or an
explicit wontfix; tracking its real state is what this tool exists to do.

The tool takes ``config/factory-build-backlog.toml`` as the source of truth
(so adding a name is one PR), partitions every entry by where it stands in
the factory today, and emits ``reports/factory-build-backlog.json`` as a
deterministic snapshot. ``--check`` exits 1 if the snapshot is stale or if
the catalog itself is malformed, so the snapshot stays trustworthy in CI.

Partitions:

- ``already_recipe``   — a directory exists under ``packages/<name>/``
- ``already_locked``   — ``config/upstream-sources.json`` has a lock entry
- ``already_packit``   — ``.packit.yaml`` has a package block
- ``manifest_wants``   — ``config/bluefin-packages.toml`` lists the name
- ``pending``          — none of the above; a factory build is still owed
- ``wontfix_resolved`` — listed in ``[wontfix]`` of the catalog

The report records every name once with the partition it falls into and a
per-area rollup. Counts always reconcile: ``total = already_recipe + (else
already_locked + (else already_packit + (else manifest_wants + (else
pending + wontfix_resolved)))))`` -- because each check is strictly weaker
than the previous, the most-specific state wins.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.package_inventory import inventory
from tools.packit_workflow import package_names


CATALOG_PATH = Path("config/factory-build-backlog.toml")
REPORT_PATH = Path("reports/factory-build-backlog.json")
PACKIT_PATH = Path(".packit.yaml")
UPSTREAM_PATH = Path("config/upstream-sources.json")
MANIFEST_PATH = Path("config/bluefin-packages.toml")

# Packit package blocks are top-level keys under `packages:`. The regex is the
# one in tools/packit_workflow.py; copy-pasted here so the audit only depends on
# the same primitive the rest of the codebase already trusts.
PACKAGE_KEY = re.compile(r"^  ([a-zA-Z0-9][a-zA-Z0-9+._-]*):$")


def _load_catalog(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _packit_names(path: Path) -> set[str]:
    """Package names declared in the root ``.packit.yaml`` ``packages:`` block.

    Duplicates ``tools.packit_workflow.package_names`` so this audit does not
    depend on Packit's load-time configuration (which is YAML, not TOML).
    """
    if not path.is_file():
        return set()
    return set(package_names(path))


def _lock_names(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    data = json.loads(path.read_text())
    return {entry["name"] for entry in data.get("packages", [])}


def _manifest_names(path: Path) -> set[str]:
    """Every package listed in any section of the Bluefin manifest."""
    if not path.is_file():
        return set()
    data = tomllib.loads(path.read_text())
    names: set[str] = set()
    for section, values in data.items():
        if section == "excluded" or not isinstance(values, dict):
            continue
        names.update(values.get("packages", []))
    return names


def _recipe_names(root: Path) -> set[str]:
    """Names with a recipe directory under ``packages/``."""
    packages = root / "packages"
    if not packages.is_dir():
        return set()
    return {d.name for d in packages.iterdir() if d.is_dir()}


def _classify(name: str, *, recipes: set[str], locks: set[str], packit: set[str], manifest: set[str], wontfix: set[str]) -> str:
    """Pick the most-specific partition a name falls into.

    Order is significant: the first match wins. ``already_recipe`` is the
    strongest signal (a full spec + provenance + sources file is present),
    then ``already_locked`` (source URL is SHA-512 pinned), then
    ``already_packit`` (Packit block declared), then ``manifest_wants``
    (consumer asks for it), then ``wontfix_resolved`` (catalog records an
    explicit decision not to carry), then ``pending`` (a factory build is
    still owed). Reordering this list silently changes counts.
    """
    if name in recipes:
        return "already_recipe"
    if name in locks:
        return "already_locked"
    if name in packit:
        return "already_packit"
    if name in manifest:
        return "manifest_wants"
    if name in wontfix:
        return "wontfix_resolved"
    return "pending"


def _catalog_totals(catalog: dict) -> tuple[set[str], dict[str, set[str]], set[str], set[str]]:
    """Pull the catalog's three name sets: backlog, wontfix, resolved."""
    backlog: dict[str, set[str]] = {}
    for area, info in catalog.get("areas", {}).items():
        backlog[area] = set(info.get("packages", []))
    resolved = {entry["name"] for entry in catalog.get("resolved", {}).get("packages", [])}
    wontfix = {entry["name"] for entry in catalog.get("wontfix", {}).get("packages", [])}
    # Anything in ``resolved`` or ``wontfix`` is no longer backlog.
    already_resolved = resolved | wontfix
    all_backlog: set[str] = set()
    for names in backlog.values():
        all_backlog.update(names)
    return all_backlog, backlog, wontfix, resolved


def _report(root: Path, catalog_path: Path) -> dict:
    catalog = _load_catalog(catalog_path)
    meta = catalog.get("meta", {})

    recipes = _recipe_names(root)
    locks = _lock_names(root / UPSTREAM_PATH.relative_to("."))
    packit = _packit_names(root / PACKIT_PATH.relative_to("."))
    manifest = _manifest_names(root / MANIFEST_PATH.relative_to("."))

    all_backlog, by_area, wontfix_set, resolved_set = _catalog_totals(catalog)

    # The catalog is the contract: any name in the backlog appears exactly once.
    seen: dict[str, int] = {}
    for area, names in by_area.items():
        for name in names:
            seen[name] = seen.get(name, 0) + 1
    duplicates = sorted(name for name, count in seen.items() if count != 1)
    if duplicates:
        raise SystemExit(
            f"catalog lists these names in more than one area: {duplicates}"
        )
    # A name that has moved out of the backlog (``[resolved]`` or ``[wontfix]``)
    # MUST NOT also appear in any area: the documented closing contract is a
    # two-edit (drop the entry, record the decision). An overlap here means
    # the next import run would silently keep counting the closed gap as
    # ``pending``.
    overlap_backlog_resolved = sorted(all_backlog & resolved_set)
    overlap_backlog_wontfix = sorted(all_backlog & wontfix_set)
    if overlap_backlog_resolved:
        raise SystemExit(
            f"catalog lists these names in both an area and [resolved]: {overlap_backlog_resolved}"
        )
    if overlap_backlog_wontfix:
        raise SystemExit(
            f"catalog lists these names in both an area and [wontfix]: {overlap_backlog_wontfix}"
        )
    # A name that has been wontfixed is, by definition, not a future build
    # candidate; it does not need a recipe and it does not need a manifest
    # decision. Marking it as both would mean we kept the door open after we
    # decided to close it.
    overlap_resolved_wontfix = sorted(resolved_set & wontfix_set)
    if overlap_resolved_wontfix:
        raise SystemExit(
            f"catalog lists these names in both [resolved] and [wontfix]: {overlap_resolved_wontfix}"
        )

    partition_counts: dict[str, int] = {}
    entries: list[dict] = []
    for area in sorted(by_area):
        for name in sorted(by_area[area]):
            state = _classify(name, recipes=recipes, locks=locks, packit=packit, manifest=manifest, wontfix=wontfix_set)
            partition_counts[state] = partition_counts.get(state, 0) + 1
            entries.append(
                {
                    "name": name,
                    "area": area,
                    "state": state,
                }
            )

    area_rollup: dict[str, dict[str, int]] = {}
    for area in sorted(by_area):
        rollup = {}
        for entry in entries:
            if entry["area"] != area:
                continue
            rollup[entry["state"]] = rollup.get(entry["state"], 0) + 1
        rollup["total"] = len(by_area[area])
        area_rollup[area] = dict(sorted(rollup.items()))

    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "issue": meta.get("issue", ""),
        "audit_source": meta.get("audit_source", ""),
        "audit_measured_at": meta.get("audit_measured_at", ""),
        "audit_digest_bluefin": meta.get("audit_digest_bluefin", ""),
        "audit_digest_utah": meta.get("audit_digest_utah", ""),
        "audit_digest_factory": meta.get("audit_digest_factory", ""),
        "totals": {
            "backlog": len(all_backlog),
            "resolved": len(resolved_set),
            "wontfix": len(wontfix_set),
        },
        "states": dict(sorted(partition_counts.items())),
        "areas": area_rollup,
        "entries": entries,
    }
    # Inventory spot-check: every name with a recipe must also be in the
    # inventory's source-locked set, not just in packages/. Mismatches here
    # mean a recipe was added without updating upstream-sources.json; this is
    # what catches a half-imported name that the backlog would otherwise call
    # resolved.
    records = inventory(root)
    packit_names = {r.name for r in records}
    inconsistent = []
    for entry in entries:
        if entry["state"] == "already_recipe" and entry["name"] not in packit_names:
            inconsistent.append(entry["name"])
    if inconsistent:
        report["inconsistent_recipe_state"] = sorted(inconsistent)
    return report


def _check(report: dict, path: Path) -> int:
    """Verify the catalog is well-formed against the current repo state.

    ``--check`` is the CI gate. It does NOT compare the full JSON snapshot
    (the ``measured_at`` timestamp legitimately drifts on every run);
    instead it verifies that:

    - every catalog name is accounted for in the report, exactly once;
    - the catalog and report partition counts reconcile;
    - no catalog name appears in more than one area.

    The committed snapshot in ``reports/factory-build-backlog.json`` is the
    durable audit output (regenerated by the recalc workflow); ``--check`` is the
    gate the workflow itself runs against the live tree.
    """
    catalog = _load_catalog(path.parent.parent / CATALOG_PATH if not path.is_absolute() else CATALOG_PATH)
    # ``catalog`` is loaded here only so the contract stays anchored to the
    # TOML catalog file; partition math already runs in ``_report``.
    del catalog

    on_disk_path = path
    if not on_disk_path.is_file():
        print(f"missing snapshot at {on_disk_path}; run without --check to regenerate")
        return 1

    # Recompute from the on-disk snapshot the same way we did live, ignoring
    # ``measured_at``: the audit's deterministic content is the catalog-vs-
    # state partitioning, which is what we gate on.
    on_disk = json.loads(on_disk_path.read_text())
    live_totals = report["totals"]
    live_states = report["states"]
    on_disk_totals = on_disk["totals"]
    on_disk_states = on_disk["states"]
    errors = []
    if live_totals != on_disk_totals:
        errors.append(f"totals drift: live {live_totals} vs snapshot {on_disk_totals}")
    if live_states != on_disk_states:
        errors.append(f"states drift: live {live_states} vs snapshot {on_disk_states}")
    # Catalog-vs-live: the catalog has ``N`` backlog names; the report's
    # entries list has exactly ``N`` items. A count mismatch is a catalog
    # regression (someone added a name without updating the report).
    backlog = report["totals"]["backlog"]
    entries = len(report["entries"])
    if entries != backlog:
        errors.append(f"entries drift: {entries} entries for {backlog} backlog names")
    if errors:
        for error in errors:
            print(error)
        return 1
    print(f"factory-build-backlog snapshot consistent ({backlog} entries, {on_disk_states.get('pending', 0)} pending)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    parser.add_argument("--check", action="store_true",
                        help="exit 1 when the committed snapshot is stale")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    catalog_path = (root / args.catalog).resolve() if not args.catalog.is_absolute() else args.catalog
    output_path = (root / args.output).resolve() if not args.output.is_absolute() else args.output

    report = _report(root, catalog_path)

    if args.check:
        return _check(report, output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    # Stable stdout summary: per-state counts, then per-area pending counts
    # so the user can see which area is the biggest gap.
    summary = {"states": report["states"], "totals": report["totals"]}
    pending_per_area = {
        area: rollup.get("pending", 0)
        for area, rollup in sorted(report["areas"].items())
    }
    summary["pending_per_area"] = pending_per_area
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
