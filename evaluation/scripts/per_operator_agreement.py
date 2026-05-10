#!/usr/bin/env python3
"""Per-operator preservation: PIT-reported mutations vs PITMuS dataset.

Inputs (read directly, no details.csv):
  test-projects/<project>/target/pit-reports/mutations.xml   -- PIT report
  PITMuS_dataset/<project>/meta.csv                          -- preserved rows

For every PIT mutation we read the operator from <mutator> and bucket by
(class-qualified source path, line). For every meta.csv row we bucket by
(source_filepath, line_no). Within each bucket the operator distribution
from XML is split proportionally onto the number of preserved dataset rows
(K / N), so per-operator counts always sum to the project-level totals.

Operators are mapped to PIT's STRONGER group (DEFAULTS + REMOVE_CONDITIONALS
+ EXPERIMENTAL_SWITCH); the four RemoveConditional sub-mutators collapse
into a single 'RemoveConditionals' row.

Outputs (under evaluation/results/):
  per_operator_preservation_<project>.csv  -- one per project
  per_operator_preservation_overall.csv    -- aggregate across all projects
"""
from __future__ import annotations

import csv
import os
import xml.etree.ElementTree as ET
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATASET_DIR = os.path.join(ROOT, "PITMuS_dataset")
TEST_PROJECTS_DIR = os.path.join(ROOT, "test-projects")
RESULTS_DIR = os.path.join(ROOT, "evaluation", "results")

# PIT mutator class (last segment) -> STRONGER display label.
OPERATOR_MAP: dict[str, str] = {
    "ConditionalsBoundaryMutator": "ConditionalsBoundary",
    "IncrementsMutator": "Increments",
    "InvertNegsMutator": "InvertNegatives",
    "MathMutator": "Math",
    "NegateConditionalsMutator": "NegateConditionals",
    "VoidMethodCallMutator": "VoidMethodCall",
    "EmptyObjectReturnValsMutator": "EmptyReturns",
    "BooleanFalseReturnValsMutator": "FalseReturns",
    "BooleanTrueReturnValsMutator": "TrueReturns",
    "NullReturnValsMutator": "NullReturns",
    "PrimitiveReturnsMutator": "PrimitiveReturns",
    "RemoveConditionalMutator_EQUAL_IF": "RemoveConditionals",
    "RemoveConditionalMutator_EQUAL_ELSE": "RemoveConditionals",
    "RemoveConditionalMutator_ORDER_IF": "RemoveConditionals",
    "RemoveConditionalMutator_ORDER_ELSE": "RemoveConditionals",
    "SwitchMutator": "ExperimentalSwitch",
    "ExperimentalSwitchMutator": "ExperimentalSwitch",
}

STRONGER_OPERATORS: list[str] = [
    "ConditionalsBoundary",
    "Increments",
    "InvertNegatives",
    "Math",
    "NegateConditionals",
    "VoidMethodCall",
    "EmptyReturns",
    "FalseReturns",
    "TrueReturns",
    "NullReturns",
    "PrimitiveReturns",
    "RemoveConditionals",
    "ExperimentalSwitch",
]


def normalize_operator(mutator_class: str) -> str | None:
    short = mutator_class.rsplit(".", 1)[-1]
    return OPERATOR_MAP.get(short)


def parse_xml_buckets(xml_path: str):
    """Return (op_total, line_buckets) for one project.

    op_total[op] = total PIT mutations with that operator (across the project).
    line_buckets[(rel_src, line)][op] = count of PIT mutations of that operator
                                        at that source/line.
    """
    op_total: dict[str, int] = defaultdict(int)
    line_buckets: dict[tuple[str, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))

    tree = ET.parse(xml_path)
    for mut in tree.getroot().findall("mutation"):
        mutator = (mut.findtext("mutator") or "").strip()
        op = normalize_operator(mutator)
        if op is None:
            continue
        cls = mut.findtext("mutatedClass") or ""
        src_basename = mut.findtext("sourceFile") or ""
        try:
            line = int(mut.findtext("lineNumber") or "0")
        except ValueError:
            line = 0
        pkg = cls.rsplit(".", 1)[0] if "." in cls else ""
        rel_src = (pkg.replace(".", "/") + "/" + src_basename) if pkg else src_basename
        op_total[op] += 1
        line_buckets[(rel_src, line)][op] += 1

    return op_total, line_buckets


def parse_dataset_buckets(meta_csv_path: str):
    """Return dataset_at_line[(rel_src, line)] = number of preserved rows."""
    counts: dict[tuple[str, int], int] = defaultdict(int)
    with open(meta_csv_path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                line = int(row["line_no"])
            except (KeyError, ValueError):
                continue
            counts[(row["source_filepath"], line)] += 1
    return counts


def project_per_operator(xml_path: str, meta_csv_path: str):
    op_total, line_buckets = parse_xml_buckets(xml_path)
    dataset_at_line = parse_dataset_buckets(meta_csv_path)

    preserved: dict[str, float] = defaultdict(float)
    for key, ops_here in line_buckets.items():
        n_xml = sum(ops_here.values())
        k_ds = dataset_at_line.get(key, 0)
        if n_xml == 0 or k_ds == 0:
            continue
        ratio = min(k_ds, n_xml) / n_xml  # cap at 1.0
        for op, c in ops_here.items():
            preserved[op] += c * ratio

    return op_total, preserved


def fmt_rate(ev: float, ag: float) -> str:
    return f"{(ag / ev) * 100:.2f}%" if ev else "--"


def write_table(out_path: str, op_total: dict[str, int], preserved: dict[str, float]):
    rows = []
    for op in STRONGER_OPERATORS:
        ev = op_total.get(op, 0)
        ag = preserved.get(op, 0.0)
        rate = (ag / ev) if ev else -1.0
        rows.append((op, ev, ag, rate))
    rows.sort(key=lambda r: (-r[3], -r[1], r[0]))

    total_ev = sum(r[1] for r in rows)
    total_ag = sum(r[2] for r in rows)

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Operator", "Eval", "Preserved", "Rate"])
        for op, ev, ag, _ in rows:
            w.writerow([op, ev, round(ag, 2), fmt_rate(ev, ag)])
        w.writerow(["Total", total_ev, round(total_ag, 2), fmt_rate(total_ev, total_ag)])
    return rows, total_ev, total_ag


def print_table(title: str, rows, total_ev, total_ag):
    print(f"\n=== {title} ===")
    print(f"{'Operator':<22} {'Eval':>8} {'Preserved':>10} {'Rate':>8}")
    print("-" * 52)
    for op, ev, ag, _ in rows:
        print(f"{op:<22} {ev:>8} {ag:>10.2f} {fmt_rate(ev, ag):>8}")
    print("-" * 52)
    print(f"{'Total':<22} {total_ev:>8} {total_ag:>10.2f} {fmt_rate(total_ev, total_ag):>8}")


def main():
    projects = sorted(
        d for d in os.listdir(DATASET_DIR)
        if os.path.isdir(os.path.join(DATASET_DIR, d))
    )

    overall_total: dict[str, int] = defaultdict(int)
    overall_preserved: dict[str, float] = defaultdict(float)
    used = []

    for p in projects:
        xml_path = os.path.join(TEST_PROJECTS_DIR, p, "target", "pit-reports", "mutations.xml")
        meta_path = os.path.join(DATASET_DIR, p, "meta.csv")
        if not os.path.isfile(xml_path):
            print(f"[skip] {p}: missing {xml_path}")
            continue
        if not os.path.isfile(meta_path):
            print(f"[skip] {p}: missing {meta_path}")
            continue

        op_total, preserved = project_per_operator(xml_path, meta_path)
        used.append(p)
        out = os.path.join(RESULTS_DIR, f"per_operator_preservation_{p}.csv")
        rows, ev, ag = write_table(out, op_total, preserved)
        print_table(p, rows, ev, ag)

        for op, n in op_total.items():
            overall_total[op] += n
        for op, n in preserved.items():
            overall_preserved[op] += n

    out = os.path.join(RESULTS_DIR, "per_operator_preservation_overall.csv")
    rows, ev, ag = write_table(out, overall_total, overall_preserved)
    print_table(f"OVERALL ({len(used)} projects)", rows, ev, ag)
    print(f"\nWrote per-project + overall CSVs to {RESULTS_DIR}")


if __name__ == "__main__":
    main()
