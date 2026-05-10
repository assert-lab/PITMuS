"""Combine per-project results into combined_summary.csv, combined_log.txt,
and overall_timing.txt under evaluation/results/.
"""
import argparse
import csv
import os
import re
from collections import Counter, defaultdict
from datetime import datetime


def read_details(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_skipped(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_timing(path):
    runs = []
    if not os.path.exists(path):
        return runs
    with open(path, encoding="utf-8") as f:
        text = f.read()
    blocks = text.split("---")
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        d = {}
        for line in b.splitlines():
            m = re.match(r"^([\w_]+):\s+(.*)$", line)
            if m:
                d[m.group(1)] = m.group(2)
        if d:
            runs.append(d)
    return runs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("projects", nargs="+")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    args = ap.parse_args()

    results_dir = os.path.join(args.root, "evaluation", "results")
    summary_csv = os.path.join(results_dir, "combined_summary.csv")
    combined_log = os.path.join(results_dir, "combined_log.txt")
    overall_timing = os.path.join(results_dir, "overall_timing.txt")

    sum_rows = []
    overall_confusion = Counter()
    overall_by_op = defaultdict(Counter)
    overall_compile_fail = 0
    overall_by_op_cfail = Counter()
    overall_skipped = Counter()
    all_durations = []  # (project, mutant_id, class, method, time_s)

    grand_eval = 0
    grand_agreed = 0
    grand_disagreed = 0
    grand_skipped = 0
    grand_total_runtime = 0.0

    for project in args.projects:
        pdir = os.path.join(results_dir, project)
        details = read_details(os.path.join(pdir, "details.csv"))
        skipped = read_skipped(os.path.join(pdir, "skipped.csv"))
        sample_path = os.path.join(args.root, "evaluation", "samples",
                                   f"sampled_{project}.csv")
        total_sampled = 0
        if os.path.exists(sample_path):
            with open(sample_path, encoding="utf-8") as f:
                total_sampled = sum(1 for _ in f) - 1

        confusion = Counter()
        by_op = defaultdict(Counter)
        cfail = 0
        by_op_cfail = Counter()
        durations = []
        for r in details:
            ag = r["agreement"]
            t = float(r.get("time_seconds", 0) or 0)
            durations.append(t)
            all_durations.append((project, r["mutant_id"], r["class"], r["method"], t))
            if ag == "compile_fail":
                cfail += 1
                by_op_cfail[r["mutator"]] += 1
                overall_compile_fail += 1
                overall_by_op_cfail[r["mutator"]] += 1
                continue
            confusion[(r["pit_label"], r["pitmus_status"])] += 1
            by_op[r["mutator"]][(r["pit_label"], r["pitmus_status"])] += 1
            overall_confusion[(r["pit_label"], r["pitmus_status"])] += 1
            overall_by_op[r["mutator"]][(r["pit_label"], r["pitmus_status"])] += 1

        agreed = confusion[("KILLED", "KILLED")] + confusion[("SURVIVED", "SURVIVED")]
        evaluated = sum(confusion.values())
        disagreed = evaluated - agreed
        rate = (agreed / evaluated) if evaluated else 0.0

        # skipped reasons
        for s in skipped:
            overall_skipped[s["reason"]] += 1

        # timing
        timings = parse_timing(os.path.join(pdir, "timing.txt"))
        proj_runtime = sum(float(t.get("total_runtime_seconds", 0)) for t in timings)
        avg = (proj_runtime / len(durations)) if durations else 0.0

        sum_rows.append({
            "project": project,
            "total_sampled": total_sampled,
            "evaluated": evaluated,
            "agreed": agreed,
            "disagreed": disagreed,
            "agreement_rate": f"{rate:.4f}",
            "compile_failures": cfail,
            "skipped_count": len(skipped),
            "total_runtime_seconds": f"{proj_runtime:.1f}",
            "avg_time_per_mutant_seconds": f"{avg:.2f}",
            "_confusion": confusion,
            "_by_op": by_op,
            "_by_op_cfail": by_op_cfail,
        })
        grand_eval += evaluated
        grand_agreed += agreed
        grand_disagreed += disagreed
        grand_skipped += len(skipped)
        grand_total_runtime += proj_runtime

    grand_total_sampled = sum(r["total_sampled"] for r in sum_rows)
    grand_cfail = sum(r["compile_failures"] for r in sum_rows)
    grand_avg = (grand_total_runtime / grand_eval) if grand_eval else 0.0
    grand_rate = (grand_agreed / grand_eval) if grand_eval else 0.0

    fields = ["project", "total_sampled", "evaluated", "agreed", "disagreed",
              "agreement_rate", "compile_failures", "skipped_count",
              "total_runtime_seconds", "avg_time_per_mutant_seconds"]
    with open(summary_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for r in sum_rows:
            w.writerow([r[k] for k in fields])
        w.writerow([
            "ALL", grand_total_sampled, grand_eval, grand_agreed, grand_disagreed,
            f"{grand_rate:.4f}", grand_cfail, grand_skipped,
            f"{grand_total_runtime:.1f}", f"{grand_avg:.2f}",
        ])

    with open(combined_log, "w", encoding="utf-8") as f:
        f.write(f"generated: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"projects: {', '.join(args.projects)}\n\n")
        f.write("=== Per-project agreement summary ===\n")
        for r in sum_rows:
            f.write(f"\n[{r['project']}]\n")
            f.write(f"  sampled={r['total_sampled']} evaluated={r['evaluated']} "
                    f"agreed={r['agreed']} rate={r['agreement_rate']} "
                    f"cfail={r['compile_failures']} skipped={r['skipped_count']}\n")
            c = r["_confusion"]
            f.write(f"  PIT_KILLED   PITMuS_KILLED   = {c[('KILLED','KILLED')]}\n")
            f.write(f"  PIT_KILLED   PITMuS_SURVIVED = {c[('KILLED','SURVIVED')]}\n")
            f.write(f"  PIT_SURVIVED PITMuS_KILLED   = {c[('SURVIVED','KILLED')]}\n")
            f.write(f"  PIT_SURVIVED PITMuS_SURVIVED = {c[('SURVIVED','SURVIVED')]}\n")

        f.write("\n=== Combined 2x2 confusion matrix ===\n")
        f.write(f"  PIT_KILLED   PITMuS_KILLED   = {overall_confusion[('KILLED','KILLED')]}\n")
        f.write(f"  PIT_KILLED   PITMuS_SURVIVED = {overall_confusion[('KILLED','SURVIVED')]}\n")
        f.write(f"  PIT_SURVIVED PITMuS_KILLED   = {overall_confusion[('SURVIVED','KILLED')]}\n")
        f.write(f"  PIT_SURVIVED PITMuS_SURVIVED = {overall_confusion[('SURVIVED','SURVIVED')]}\n")
        f.write(f"  agreement_rate = {grand_rate:.4f}\n")

        f.write("\n=== Per-operator agreement (combined) ===\n")
        f.write(f"  {'operator':<45}{'eval':>6}{'agree':>7}{'rate':>8}{'cfail':>7}\n")
        for op in sorted(set(list(overall_by_op.keys()) + list(overall_by_op_cfail.keys()))):
            sub = overall_by_op[op]
            t = sum(sub.values())
            a = sub[("KILLED", "KILLED")] + sub[("SURVIVED", "SURVIVED")]
            rate = (a / t) if t else 0.0
            f.write(f"  {op:<45}{t:>6}{a:>7}{rate:>8.4f}{overall_by_op_cfail[op]:>7}\n")

        f.write("\n=== Skipped reasons (combined) ===\n")
        for k, v in overall_skipped.most_common():
            f.write(f"  {k}: {v}\n")

    # overall_timing.txt
    project_times = [(r["project"], r["total_runtime_seconds"]) for r in sum_rows]
    slowest = sorted(all_durations, key=lambda x: x[4], reverse=True)[:20]
    with open(overall_timing, "w", encoding="utf-8") as f:
        f.write(f"generated: {datetime.now().isoformat(timespec='seconds')}\n\n")
        for proj, runtime_s in project_times:
            f.write(f"{proj}: total_runtime={runtime_s}s\n")
        f.write(f"\ntotal_wall_clock_seconds: {grand_total_runtime:.1f}\n")
        f.write(f"avg_time_per_mutant_seconds: {grand_avg:.2f}\n")
        f.write(f"\n=== Slowest 20 mutants overall ===\n")
        for proj, mid, cls, meth, t in slowest:
            f.write(f"  {t:.2f}s  {proj}  {mid}  {cls}::{meth}\n")

    print(f"[done] {summary_csv}")
    print(f"[done] {combined_log}")
    print(f"[done] {overall_timing}")


if __name__ == "__main__":
    main()
