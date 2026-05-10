"""Evaluate PITMuS source-mutant fidelity against PIT bytecode-mutant labels.

For each PIT mutant in target/pit-reports/mutations.xml:
  1. Match it to a PITMuS row in mutated_src_lines/{Class}.csv by
     (sourceFile, lineNumber, description).
  2. Inject the PITMuS mutated_line into the source file.
  3. Run the same coveringTests PIT recorded.
  4. Outcome = KILLED if any test fails OR compilation fails, else SURVIVED.
  5. Compare to PIT's status; report agreement.
"""
import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, Counter


PIT_KILLED_STATUSES = {"KILLED", "TIMED_OUT", "MEMORY_ERROR", "RUN_ERROR"}
PIT_SURVIVED_STATUSES = {"SURVIVED"}
PIT_SKIP_STATUSES = {"NO_COVERAGE"}

TEST_ENTRY_RE = re.compile(r"^([\w\.\$]+)\.(\w+)\(([\w\.\$]+)\)$")


def parse_test_entry(s):
    """Return (class, method) or (class, None) for a coveringTests entry."""
    m = TEST_ENTRY_RE.match(s)
    if m:
        return m.group(1), m.group(2)
    return s, None


def covering_tests_to_dtest(covering_tests):
    """Convert PIT's coveringTests string to surefire -Dtest argument."""
    full_class = set()
    by_class = defaultdict(set)
    for entry in covering_tests.split("|"):
        if not entry:
            continue
        cls, meth = parse_test_entry(entry)
        if meth is None:
            full_class.add(cls)
        else:
            by_class[cls].add(meth)
    parts = []
    for cls in full_class:
        parts.append(cls)
    for cls, methods in by_class.items():
        if cls in full_class:
            continue
        parts.append(cls + "#" + "+".join(sorted(methods)))
    return ",".join(parts) if parts else None


def parse_pit_xml(xml_path):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    out = []
    for m in root.findall("mutation"):
        out.append({
            "status": m.get("status"),
            "sourceFile": m.findtext("sourceFile"),
            "mutatedClass": m.findtext("mutatedClass"),
            "mutatedMethod": m.findtext("mutatedMethod"),
            "lineNumber": int(m.findtext("lineNumber")),
            "mutator": m.findtext("mutator").split(".")[-1],
            "description": m.findtext("description"),
            "coveringTests": m.findtext("coveringTests") or "",
        })
    return out


def load_pitmus_rows(project_dir):
    """Build {(source_file, line_number, description): [row, ...]} from
    test-projects/{project}/mutated_src_lines/*.csv."""
    out = defaultdict(list)
    csv_dir = os.path.join(project_dir, "mutated_src_lines")
    for fn in sorted(os.listdir(csv_dir)):
        if not fn.endswith(".csv"):
            continue
        with open(os.path.join(csv_dir, fn), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row["source_file"], int(row["line_number"]), row["description"])
                out[key].append(row)
    return out


def class_to_source_file(mutated_class):
    """org.foo.Bar$Inner -> org/foo/Bar.java"""
    top = mutated_class.split("$", 1)[0]
    return top.replace(".", "/") + ".java"


def find_source_path(src_root, source_filepath):
    abs_path = os.path.join(src_root, source_filepath.replace("/", os.sep))
    if os.path.exists(abs_path):
        return abs_path
    base = os.path.basename(source_filepath)
    for root, _, files in os.walk(src_root):
        if base in files:
            return os.path.join(root, base)
    return None


def inject_mutation(abs_src_path, line_number, mutated_line):
    with open(abs_src_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    idx = line_number - 1
    if not (0 <= idx < len(lines)):
        return False, None
    original = lines[idx]
    indent = len(original) - len(original.lstrip())
    new_line = " " * indent + mutated_line.strip() + "\n"
    new_lines = list(lines)
    new_lines[idx] = new_line
    with open(abs_src_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    return True, "".join(lines)


def run_mvn_test(project_dir, dtest, timeout):
    cmd = [
        "mvn", "-q", "-o", "test",
        "-DfailIfNoTests=false",
        f"-Dtest={dtest}",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=project_dir, capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        def _to_str(x):
            if x is None:
                return ""
            if isinstance(x, bytes):
                return x.decode("utf-8", errors="replace")
            return x
        return 124, _to_str(e.stdout), _to_str(e.stderr)


def classify_outcome(returncode, stdout, stderr):
    """Return (pitmus_status, detail).

    pitmus_status in {KILLED, SURVIVED, COMPILE_FAIL, NO_TESTS_RUN}.
    """
    out = (stdout or "") + "\n" + (stderr or "")
    if returncode == 124:
        return "KILLED", "timeout"
    if "BUILD FAILURE" in out and ("COMPILATION ERROR" in out or "compilation error" in out.lower()):
        return "COMPILE_FAIL", "compilation error"
    if returncode != 0:
        # surefire test failures usually surface as BUILD FAILURE with
        # 'There are test failures'.
        if "There are test failures" in out or "Failed tests" in out or "Tests run" in out:
            return "KILLED", "test failure"
        # Generic non-zero: treat as KILLED but flag it
        return "KILLED", f"non-zero rc={returncode}"
    # Success
    # Check that any tests actually ran
    if "Tests run: 0" in out and "Tests run: " in out and "Tests run: 0," in out and "Failures: 0" in out:
        # ambiguous; treat as no tests
        return "NO_TESTS_RUN", "no tests run"
    return "SURVIVED", "ok"


def pit_label(status):
    if status in PIT_KILLED_STATUSES:
        return "KILLED"
    if status in PIT_SURVIVED_STATUSES:
        return "SURVIVED"
    return None  # skip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project", help="project name under test-projects/")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="PITMuS root (default: parent of scripts/)")
    ap.add_argument("--limit", type=int, default=None, help="evaluate at most N PIT mutants")
    ap.add_argument("--mutator", action="append", default=None,
                    help="filter: only evaluate mutants whose PIT mutator matches (repeatable)")
    ap.add_argument("--timeout", type=int, default=120, help="per-mutant mvn timeout (s)")
    ap.add_argument("--out-dir", default=None,
                    help="output directory (default: test-projects/{project}/agreement/)")
    ap.add_argument("--resume", action="store_true",
                    help="skip mutants already present in details.csv")
    args = ap.parse_args()

    project_dir = os.path.join(args.root, "test-projects", args.project)
    if not os.path.isdir(project_dir):
        sys.exit(f"not a project: {project_dir}")

    src_root = os.path.join(project_dir, "src", "main", "java")
    pit_xml = os.path.join(project_dir, "target", "pit-reports", "mutations.xml")
    if not os.path.exists(pit_xml):
        sys.exit(f"missing PIT report: {pit_xml}")

    out_dir = args.out_dir or os.path.join(project_dir, "agreement")
    os.makedirs(out_dir, exist_ok=True)
    details_path = os.path.join(out_dir, "details.csv")
    skipped_path = os.path.join(out_dir, "skipped.csv")
    summary_proj_path = os.path.join(out_dir, "by_project.csv")
    summary_op_path = os.path.join(out_dir, "by_operator.csv")

    pit_mutants = parse_pit_xml(pit_xml)
    pitmus_index = load_pitmus_rows(project_dir)

    print(f"[info] {len(pit_mutants)} PIT mutants, {sum(len(v) for v in pitmus_index.values())} PITMuS rows", flush=True)

    # filtering
    if args.mutator:
        keep = set(args.mutator)
        pit_mutants = [m for m in pit_mutants if m["mutator"] in keep]
        print(f"[info] mutator filter -> {len(pit_mutants)} mutants", flush=True)

    # resume support
    already_done = set()
    if args.resume and os.path.exists(details_path):
        with open(details_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                already_done.add((row["mutatedClass"], row["lineNumber"], row["description"]))
        print(f"[info] resume: {len(already_done)} mutants already evaluated", flush=True)

    # output files (append if resuming)
    details_existed = os.path.exists(details_path) and args.resume
    skipped_existed = os.path.exists(skipped_path) and args.resume
    f_details = open(details_path, "a" if details_existed else "w", newline="", encoding="utf-8")
    f_skipped = open(skipped_path, "a" if skipped_existed else "w", newline="", encoding="utf-8")
    details_writer = csv.writer(f_details)
    skipped_writer = csv.writer(f_skipped)
    if not details_existed:
        details_writer.writerow([
            "mutatedClass", "sourceFile", "lineNumber", "mutator", "description",
            "pit_status", "pit_label", "pitmus_status", "agreed", "detail",
            "duration_s",
        ])
    if not skipped_existed:
        skipped_writer.writerow([
            "mutatedClass", "sourceFile", "lineNumber", "mutator", "description",
            "pit_status", "reason",
        ])

    n_eval = 0
    n_skipped = 0
    started = time.time()

    for i, m in enumerate(pit_mutants):
        if args.limit is not None and n_eval >= args.limit:
            break

        label = pit_label(m["status"])
        source_filepath = class_to_source_file(m["mutatedClass"])
        key = (source_filepath, m["lineNumber"], m["description"])

        if label is None:
            skipped_writer.writerow([m["mutatedClass"], source_filepath, m["lineNumber"],
                                     m["mutator"], m["description"], m["status"],
                                     "pit_status_skipped"])
            n_skipped += 1
            continue

        if (m["mutatedClass"], str(m["lineNumber"]), m["description"]) in already_done:
            continue

        rows = pitmus_index.get(key)
        if not rows:
            skipped_writer.writerow([m["mutatedClass"], source_filepath, m["lineNumber"],
                                     m["mutator"], m["description"], m["status"],
                                     "no_pitmus_match"])
            n_skipped += 1
            continue
        if len(rows) > 1:
            skipped_writer.writerow([m["mutatedClass"], source_filepath, m["lineNumber"],
                                     m["mutator"], m["description"], m["status"],
                                     f"ambiguous_pitmus_match_{len(rows)}"])
            n_skipped += 1
            continue
        pitmus_row = rows[0]

        abs_src = find_source_path(src_root, source_filepath)
        if not abs_src:
            skipped_writer.writerow([m["mutatedClass"], source_filepath, m["lineNumber"],
                                     m["mutator"], m["description"], m["status"],
                                     "source_not_found"])
            n_skipped += 1
            continue

        dtest = covering_tests_to_dtest(m["coveringTests"])
        if not dtest:
            skipped_writer.writerow([m["mutatedClass"], source_filepath, m["lineNumber"],
                                     m["mutator"], m["description"], m["status"],
                                     "no_covering_tests"])
            n_skipped += 1
            continue

        # inject, run, restore
        ok, original_text = inject_mutation(abs_src, m["lineNumber"], pitmus_row["mutated_line"])
        if not ok:
            skipped_writer.writerow([m["mutatedClass"], source_filepath, m["lineNumber"],
                                     m["mutator"], m["description"], m["status"],
                                     "inject_out_of_range"])
            n_skipped += 1
            continue

        t0 = time.time()
        try:
            rc, stdout, stderr = run_mvn_test(project_dir, dtest, args.timeout)
            pitmus_status, detail = classify_outcome(rc, stdout, stderr)
        finally:
            with open(abs_src, "w", encoding="utf-8") as fh:
                fh.write(original_text)
        dur = time.time() - t0

        if pitmus_status in ("COMPILE_FAIL", "NO_TESTS_RUN"):
            agreed = ""
        else:
            agreed = "1" if pitmus_status == label else "0"

        details_writer.writerow([
            m["mutatedClass"], source_filepath, m["lineNumber"], m["mutator"], m["description"],
            m["status"], label, pitmus_status, agreed, detail, f"{dur:.2f}",
        ])
        f_details.flush()
        n_eval += 1

        if n_eval % 10 == 0 or n_eval == 1:
            elapsed = time.time() - started
            print(f"[progress] {n_eval} evaluated, {n_skipped} skipped, {elapsed:.1f}s elapsed "
                  f"(last: {m['mutator']} L{m['lineNumber']} -> {pitmus_status}, agreed={agreed})",
                  flush=True)

    f_details.close()
    f_skipped.close()

    # aggregate
    aggregate(details_path, args.project, summary_proj_path, summary_op_path)

    print(f"\n[done] evaluated={n_eval}, skipped={n_skipped}, "
          f"total_elapsed={time.time()-started:.1f}s")
    print(f"[done] details: {details_path}")
    print(f"[done] by-project: {summary_proj_path}")
    print(f"[done] by-operator: {summary_op_path}")


def aggregate(details_path, project, by_project_path, by_op_path):
    if not os.path.exists(details_path):
        return
    proj_total = 0
    proj_agree = 0
    proj_compile_fail = 0
    proj_no_tests = 0
    by_op_total = Counter()
    by_op_agree = Counter()
    by_op_compile_fail = Counter()
    by_op_no_tests = Counter()
    confusion_proj = Counter()  # (pit_label, pitmus_status)
    confusion_by_op = defaultdict(Counter)

    with open(details_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            op = row["mutator"]
            ps = row["pitmus_status"]
            confusion_proj[(row["pit_label"], ps)] += 1
            confusion_by_op[op][(row["pit_label"], ps)] += 1
            if ps == "COMPILE_FAIL":
                proj_compile_fail += 1
                by_op_compile_fail[op] += 1
                continue
            if ps == "NO_TESTS_RUN":
                proj_no_tests += 1
                by_op_no_tests[op] += 1
                continue
            proj_total += 1
            by_op_total[op] += 1
            if row["agreed"] == "1":
                proj_agree += 1
                by_op_agree[op] += 1

    with open(by_project_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["project", "evaluated", "agreed", "agreement_rate",
                    "compile_fail", "no_tests_run",
                    "pit_killed_pitmus_killed", "pit_killed_pitmus_survived",
                    "pit_survived_pitmus_killed", "pit_survived_pitmus_survived"])
        rate = (proj_agree / proj_total) if proj_total else 0.0
        w.writerow([
            project, proj_total, proj_agree, f"{rate:.4f}",
            proj_compile_fail, proj_no_tests,
            confusion_proj[("KILLED", "KILLED")], confusion_proj[("KILLED", "SURVIVED")],
            confusion_proj[("SURVIVED", "KILLED")], confusion_proj[("SURVIVED", "SURVIVED")],
        ])

    with open(by_op_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["project", "operator", "evaluated", "agreed", "agreement_rate",
                    "compile_fail", "no_tests_run",
                    "pit_killed_pitmus_killed", "pit_killed_pitmus_survived",
                    "pit_survived_pitmus_killed", "pit_survived_pitmus_survived"])
        for op in sorted(set(list(by_op_total.keys()) + list(by_op_compile_fail.keys()) + list(by_op_no_tests.keys()))):
            t = by_op_total[op]
            a = by_op_agree[op]
            rate = (a / t) if t else 0.0
            w.writerow([
                project, op, t, a, f"{rate:.4f}",
                by_op_compile_fail[op], by_op_no_tests[op],
                confusion_by_op[op][("KILLED", "KILLED")],
                confusion_by_op[op][("KILLED", "SURVIVED")],
                confusion_by_op[op][("SURVIVED", "KILLED")],
                confusion_by_op[op][("SURVIVED", "SURVIVED")],
            ])


if __name__ == "__main__":
    main()
