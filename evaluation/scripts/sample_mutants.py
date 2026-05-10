"""Stratified random sample of PIT mutants for one project.

Loads test-projects/{project}/target/pit-reports/mutations.xml, drops NO_COVERAGE
mutants, groups by (class + method + methodDescription), keeps methods with at
least 5 mutants, samples 100 methods (or all if fewer) and 5 mutants per method.

Seed is fixed at 42 for reproducibility.
"""
import argparse
import csv
import hashlib
import os
import random
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict


SEED = 42
PIT_SKIP = {"NO_COVERAGE"}
N_METHODS = 100
N_PER_METHOD = 5


def stable_id(parts):
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def load_pit(xml_path, project):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    rows = []
    for m in root.findall("mutation"):
        status = m.get("status")
        if status in PIT_SKIP:
            continue
        d = {
            "project": project,
            "class": m.findtext("mutatedClass") or "",
            "method": m.findtext("mutatedMethod") or "",
            "methodDescription": m.findtext("methodDescription") or "",
            "line": int(m.findtext("lineNumber") or 0),
            "mutator": (m.findtext("mutator") or "").split(".")[-1],
            "description": m.findtext("description") or "",
            "sourceFile": m.findtext("sourceFile") or "",
            "coveringTests": m.findtext("coveringTests") or "",
            "pit_status": status,
        }
        d["mutant_id"] = stable_id([d["class"], d["method"], d["methodDescription"],
                                    str(d["line"]), d["mutator"], d["description"]])
        rows.append(d)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    pit_xml = os.path.join(args.root, "test-projects", args.project,
                           "target", "pit-reports", "mutations.xml")
    if not os.path.exists(pit_xml):
        print(f"[error] missing PIT report: {pit_xml}", file=sys.stderr)
        sys.exit(2)

    out_dir = args.out_dir or os.path.join(args.root, "evaluation", "samples")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"sampled_{args.project}.csv")

    rows = load_pit(pit_xml, args.project)
    print(f"[info] {args.project}: {len(rows)} PIT mutants after dropping NO_COVERAGE")

    by_method = defaultdict(list)
    for r in rows:
        by_method[(r["class"], r["method"], r["methodDescription"])].append(r)

    eligible = [k for k, v in by_method.items() if len(v) >= N_PER_METHOD]
    eligible.sort()
    print(f"[info] {args.project}: {len(by_method)} methods total, "
          f"{len(eligible)} eligible (>= {N_PER_METHOD} mutants)")

    rng = random.Random(SEED)
    if len(eligible) < N_METHODS:
        print(f"[warning] {args.project}: only {len(eligible)} eligible methods "
              f"(< {N_METHODS}); using all of them")
        chosen_methods = list(eligible)
    else:
        chosen_methods = rng.sample(eligible, N_METHODS)

    sampled = []
    for key in chosen_methods:
        muts = sorted(by_method[key], key=lambda d: d["mutant_id"])
        if len(muts) > N_PER_METHOD:
            sampled.extend(rng.sample(muts, N_PER_METHOD))
        else:
            sampled.extend(muts)

    fields = [
        "project", "class", "method", "methodDescription", "line", "mutator",
        "description", "sourceFile", "coveringTests", "pit_status", "mutant_id",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for d in sampled:
            w.writerow([d[k] for k in fields])

    print(f"[done] wrote {len(sampled)} sampled mutants to {out_path}")
    print(f"[done] methods sampled: {len(chosen_methods)}")


if __name__ == "__main__":
    main()
