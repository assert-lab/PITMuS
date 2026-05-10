"""Run PITMuS-vs-PIT agreement evaluation against a sampled CSV.

Reads evaluation/samples/sampled_{project}.csv. For each mutant:
  1. Look up PITMuS row by (sourceFile, line, description) in
     test-projects/{project}/mutated_src_lines/*.csv.
  2. Inject PITMuS mutated_line into the source file.
  3. Run PIT's coveringTests via mvn.
  4. Classify outcome and compare to PIT's status.
  5. Restore source.

Writes details.csv, skipped.csv, run_log.txt, timing.txt and raw
mvn output for each disagreement to disagreement_logs/{mutant_id}.log.
Supports --resume to skip rows already in details.csv.
"""
import argparse
import csv
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, Counter
from datetime import datetime


PIT_KILLED = {"KILLED", "TIMED_OUT", "MEMORY_ERROR", "RUN_ERROR"}
PIT_SURVIVED = {"SURVIVED"}

TEST_ENTRY_RE = re.compile(r"^([\w\.\$]+)\.(\w+)\(([\w\.\$]+)\)$")


def parse_test_entry(s):
    m = TEST_ENTRY_RE.match(s)
    if m:
        return m.group(1), m.group(2)
    return s, None


def covering_tests_to_dtest(covering_tests):
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
    parts = list(full_class)
    for cls, methods in by_class.items():
        if cls in full_class:
            continue
        parts.append(cls + "#" + "+".join(sorted(methods)))
    return ",".join(parts) if parts else None


def class_to_source_file(mutated_class):
    return mutated_class.split("$", 1)[0].replace(".", "/") + ".java"


def find_source_path(src_root, source_filepath):
    abs_path = os.path.join(src_root, source_filepath.replace("/", os.sep))
    if os.path.exists(abs_path):
        return abs_path
    base = os.path.basename(source_filepath)
    for root, _, files in os.walk(src_root):
        if base in files:
            return os.path.join(root, base)
    return None


def load_pitmus_index(project_dir):
    out = defaultdict(list)
    csv_dir = os.path.join(project_dir, "mutated_src_lines")
    if not os.path.isdir(csv_dir):
        return out
    for fn in sorted(os.listdir(csv_dir)):
        if not fn.endswith(".csv"):
            continue
        with open(os.path.join(csv_dir, fn), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row["source_file"], int(row["line_number"]), row["description"])
                out[key].append(row)
    return out


def inject_mutation(abs_src_path, line_number, mutated_line):
    with open(abs_src_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    idx = line_number - 1
    if not (0 <= idx < len(lines)):
        return False, None
    original = lines[idx]
    indent = len(original) - len(original.lstrip())
    new_lines = list(lines)
    new_lines[idx] = " " * indent + mutated_line.strip() + "\n"
    with open(abs_src_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    return True, "".join(lines)


def to_str(x):
    if x is None:
        return ""
    if isinstance(x, bytes):
        return x.decode("utf-8", errors="replace")
    return x


def run_mvn_test(project_dir, dtest, timeout):
    cmd = ["mvn", "-q", "-o", "test", "-DfailIfNoTests=false", f"-Dtest={dtest}"]
    try:
        proc = subprocess.run(cmd, cwd=project_dir, capture_output=True,
                              text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return 124, to_str(e.stdout), to_str(e.stderr)


def classify_outcome(rc, stdout, stderr):
    out = (stdout or "") + "\n" + (stderr or "")
    if rc == 124:
        return "KILLED", "timeout"
    if "BUILD FAILURE" in out and ("COMPILATION ERROR" in out or "compilation error" in out.lower()):
        return "COMPILE_FAIL", "compile_error"
    if rc != 0:
        if "There are test failures" in out or "Failed tests" in out or "Tests run" in out:
            return "KILLED", "test_failure"
        return "KILLED", f"non_zero_rc_{rc}"
    return "SURVIVED", "ok"


def pit_label(status):
    if status in PIT_KILLED:
        return "KILLED"
    if status in PIT_SURVIVED:
        return "SURVIVED"
    return None


def write_disagreement_log(disagreement_dir, mutant_id, sample_row, pitmus_status,
                           detail, rc, stdout, stderr):
    os.makedirs(disagreement_dir, exist_ok=True)
    path = os.path.join(disagreement_dir, f"{mutant_id}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"mutant_id={mutant_id}\n")
        for k in ["class", "method", "methodDescription", "line", "mutator",
                  "description", "sourceFile", "pit_status"]:
            f.write(f"{k}={sample_row.get(k, '')}\n")
        f.write(f"pitmus_status={pitmus_status}\n")
        f.write(f"detail={detail}\n")
        f.write(f"return_code={rc}\n")
        f.write("--- STDOUT ---\n")
        f.write(stdout or "")
        f.write("\n--- STDERR ---\n")
        f.write(stderr or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    ap.add_argument("--sample-file", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    project_dir = os.path.join(args.root, "test-projects", args.project)
    src_root = os.path.join(project_dir, "src", "main", "java")
    sample_file = args.sample_file or os.path.join(
        args.root, "evaluation", "samples", f"sampled_{args.project}.csv")
    out_dir = args.out_dir or os.path.join(
        args.root, "evaluation", "results", args.project)
    os.makedirs(out_dir, exist_ok=True)
    disagreement_dir = os.path.join(out_dir, "disagreement_logs")

    details_path = os.path.join(out_dir, "details.csv")
    skipped_path = os.path.join(out_dir, "skipped.csv")
    log_path = os.path.join(out_dir, "run_log.txt")
    timing_path = os.path.join(out_dir, "timing.txt")
    summary_path = os.path.join(out_dir, "agreement_summary.txt")

    if not os.path.exists(sample_file):
        sys.exit(f"missing sample file: {sample_file}")
    if not os.path.exists(project_dir):
        sys.exit(f"missing project dir: {project_dir}")

    pitmus_index = load_pitmus_index(project_dir)

    with open(sample_file, newline="", encoding="utf-8") as f:
        sampled = list(csv.DictReader(f))

    already_done = set()
    if args.resume and os.path.exists(details_path):
        with open(details_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                already_done.add(row["mutant_id"])
    if args.resume and os.path.exists(skipped_path):
        with open(skipped_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                already_done.add(row["mutant_id"])

    log_mode = "a" if (args.resume and os.path.exists(log_path)) else "w"
    log_f = open(log_path, log_mode, encoding="utf-8")

    def log(msg):
        ts = datetime.now().isoformat(timespec="seconds")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        log_f.write(line + "\n")
        log_f.flush()

    log(f"start project={args.project} sampled={len(sampled)} resume={args.resume} "
        f"already_done={len(already_done)}")

    details_existed = args.resume and os.path.exists(details_path)
    skipped_existed = args.resume and os.path.exists(skipped_path)
    f_details = open(details_path, "a" if details_existed else "w",
                     newline="", encoding="utf-8")
    f_skipped = open(skipped_path, "a" if skipped_existed else "w",
                     newline="", encoding="utf-8")
    w_details = csv.writer(f_details)
    w_skipped = csv.writer(f_skipped)
    if not details_existed:
        w_details.writerow([
            "project", "mutant_id", "class", "method", "line", "mutator",
            "description", "pit_status", "pit_label", "pitmus_status",
            "agreement", "detail", "time_seconds",
        ])
    if not skipped_existed:
        w_skipped.writerow([
            "project", "mutant_id", "class", "method", "line", "mutator",
            "description", "pit_status", "reason",
        ])

    started = time.time()
    n_eval = 0
    n_skipped = 0
    durations = []  # (mutant_id, class, method, time_s)

    for row in sampled:
        if row["mutant_id"] in already_done:
            continue

        label = pit_label(row["pit_status"])
        if label is None:
            w_skipped.writerow([row["project"], row["mutant_id"], row["class"],
                                row["method"], row["line"], row["mutator"],
                                row["description"], row["pit_status"], "pit_status_skipped"])
            f_skipped.flush()
            n_skipped += 1
            continue

        source_filepath = row["sourceFile"]
        if not source_filepath:
            source_filepath = class_to_source_file(row["class"])
        else:
            # PIT XML's sourceFile is just the basename; rebuild from class
            source_filepath = class_to_source_file(row["class"])

        key = (source_filepath, int(row["line"]), row["description"])
        candidates = pitmus_index.get(key, [])
        if not candidates:
            w_skipped.writerow([row["project"], row["mutant_id"], row["class"],
                                row["method"], row["line"], row["mutator"],
                                row["description"], row["pit_status"], "no_pitmus_match"])
            f_skipped.flush()
            n_skipped += 1
            continue
        if len(candidates) > 1:
            w_skipped.writerow([row["project"], row["mutant_id"], row["class"],
                                row["method"], row["line"], row["mutator"],
                                row["description"], row["pit_status"],
                                f"ambiguous_pitmus_match_{len(candidates)}"])
            f_skipped.flush()
            n_skipped += 1
            continue
        pitmus_row = candidates[0]

        abs_src = find_source_path(src_root, source_filepath)
        if not abs_src:
            w_skipped.writerow([row["project"], row["mutant_id"], row["class"],
                                row["method"], row["line"], row["mutator"],
                                row["description"], row["pit_status"], "source_not_found"])
            f_skipped.flush()
            n_skipped += 1
            continue

        dtest = covering_tests_to_dtest(row["coveringTests"])
        if not dtest:
            w_skipped.writerow([row["project"], row["mutant_id"], row["class"],
                                row["method"], row["line"], row["mutator"],
                                row["description"], row["pit_status"], "no_covering_tests"])
            f_skipped.flush()
            n_skipped += 1
            continue

        ok, original_text = inject_mutation(abs_src, int(row["line"]),
                                            pitmus_row["mutated_line"])
        if not ok:
            w_skipped.writerow([row["project"], row["mutant_id"], row["class"],
                                row["method"], row["line"], row["mutator"],
                                row["description"], row["pit_status"], "inject_out_of_range"])
            f_skipped.flush()
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

        if pitmus_status in ("COMPILE_FAIL",):
            agreement = "compile_fail"
        else:
            agreement = "agreed" if pitmus_status == label else "disagreed"

        if agreement == "disagreed":
            write_disagreement_log(disagreement_dir, row["mutant_id"], row,
                                   pitmus_status, detail, rc, stdout, stderr)

        w_details.writerow([
            row["project"], row["mutant_id"], row["class"], row["method"],
            row["line"], row["mutator"], row["description"],
            row["pit_status"], label, pitmus_status, agreement, detail,
            f"{dur:.2f}",
        ])
        f_details.flush()
        n_eval += 1
        durations.append((row["mutant_id"], row["class"], row["method"], dur))

        if n_eval % 25 == 0 or n_eval == 1:
            elapsed = time.time() - started
            log(f"progress evaluated={n_eval} skipped={n_skipped} "
                f"elapsed={elapsed:.1f}s last={row['mutator']} L{row['line']} "
                f"-> {pitmus_status} {agreement}")

    f_details.close()
    f_skipped.close()
    total = time.time() - started
    log(f"done evaluated={n_eval} skipped={n_skipped} elapsed={total:.1f}s")

    write_summary(args.project, details_path, skipped_path, summary_path)
    write_timing(timing_path, durations, total, args.resume)

    log(f"summary at {summary_path}")
    log_f.close()


def write_summary(project, details_path, skipped_path, summary_path):
    confusion = Counter()
    by_op = defaultdict(Counter)
    compile_fail = 0
    by_op_compile_fail = Counter()
    total = 0

    if os.path.exists(details_path):
        with open(details_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                op = row["mutator"]
                ag = row["agreement"]
                if ag == "compile_fail":
                    compile_fail += 1
                    by_op_compile_fail[op] += 1
                    continue
                total += 1
                confusion[(row["pit_label"], row["pitmus_status"])] += 1
                by_op[op][(row["pit_label"], row["pitmus_status"])] += 1

    skipped_reasons = Counter()
    if os.path.exists(skipped_path):
        with open(skipped_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                skipped_reasons[row["reason"]] += 1

    agreed = confusion[("KILLED", "KILLED")] + confusion[("SURVIVED", "SURVIVED")]
    rate = (agreed / total) if total else 0.0

    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"project: {project}\n")
        f.write(f"evaluated: {total}\n")
        f.write(f"agreed: {agreed}\n")
        f.write(f"agreement_rate: {rate:.4f}\n")
        f.write(f"compile_failures: {compile_fail}\n")
        f.write(f"skipped: {sum(skipped_reasons.values())}\n")
        f.write("\nConfusion (PIT vs PITMuS):\n")
        f.write(f"  PIT_KILLED   PITMuS_KILLED   = {confusion[('KILLED','KILLED')]}\n")
        f.write(f"  PIT_KILLED   PITMuS_SURVIVED = {confusion[('KILLED','SURVIVED')]}\n")
        f.write(f"  PIT_SURVIVED PITMuS_KILLED   = {confusion[('SURVIVED','KILLED')]}\n")
        f.write(f"  PIT_SURVIVED PITMuS_SURVIVED = {confusion[('SURVIVED','SURVIVED')]}\n")
        f.write("\nPer-operator agreement:\n")
        f.write(f"  {'operator':<45}{'eval':>6}{'agree':>7}{'rate':>8}{'cfail':>7}\n")
        for op in sorted(set(list(by_op.keys()) + list(by_op_compile_fail.keys()))):
            sub = by_op[op]
            t = sum(sub.values())
            a = sub[("KILLED", "KILLED")] + sub[("SURVIVED", "SURVIVED")]
            r = (a / t) if t else 0.0
            f.write(f"  {op:<45}{t:>6}{a:>7}{r:>8.4f}{by_op_compile_fail[op]:>7}\n")
        f.write("\nSkipped reasons:\n")
        for k, v in skipped_reasons.most_common():
            f.write(f"  {k}: {v}\n")


def write_timing(timing_path, durations, total, append):
    mode = "a" if append and os.path.exists(timing_path) else "w"
    n = len(durations)
    avg = (sum(d[3] for d in durations) / n) if n else 0.0
    slow = sorted(durations, key=lambda x: x[3], reverse=True)[:10]
    with open(timing_path, mode, encoding="utf-8") as f:
        f.write(f"---\nrun_at: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"total_runtime_seconds: {total:.1f}\n")
        f.write(f"new_mutants_evaluated: {n}\n")
        f.write(f"avg_seconds_per_mutant: {avg:.2f}\n")
        f.write("slowest 10 (this run):\n")
        for mid, cls, meth, t in slow:
            f.write(f"  {t:.2f}s  {mid}  {cls}::{meth}\n")


if __name__ == "__main__":
    main()
