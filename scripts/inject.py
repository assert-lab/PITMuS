#!/usr/bin/env python3
import csv
import os
import sys

import javalang
from javalang.tokenizer import LexerError


def inject_mutation(lines, line_number, mutated_line):
    result = list(lines)
    idx = line_number - 1
    if 0 <= idx < len(result):
        indent = len(result[idx]) - len(result[idx].lstrip())
        result[idx] = " " * indent + mutated_line.strip() + "\n"
    return result


def validate_syntax(source_text):
    try:
        javalang.tokenizer.tokenize(source_text)
        return True
    except (LexerError, StopIteration, Exception):
        return False


def load_mutations(csv_dir, source_file=None, line_number=None):
    matches = []
    for csv_file in sorted(os.listdir(csv_dir)):
        if not csv_file.endswith(".csv"):
            continue
        with open(os.path.join(csv_dir, csv_file), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if source_file and not row["source_file"].endswith(source_file):
                    continue
                if line_number is not None and int(row["line_number"]) != line_number:
                    continue
                matches.append(row)
    return matches


def find_source(src_root, filename):
    for root, _, files in os.walk(src_root):
        if filename in files:
            return os.path.join(root, filename)
    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python inject_mutated_src_code.py <project> [source-file] [line-number]")
        print("  python inject_mutated_src_code.py joda-time")
        print("  python inject_mutated_src_code.py joda-time PeriodFormatterBuilder.java")
        print("  python inject_mutated_src_code.py joda-time PeriodFormatterBuilder.java 1377")
        sys.exit(1)

    project = os.path.join("test-projects", sys.argv[1].rstrip("/"))
    source_file = sys.argv[2] if len(sys.argv) > 2 else None
    line_number = int(sys.argv[3]) if len(sys.argv) > 3 else None

    src_root = os.path.join(project, "src", "main", "java")
    csv_dir = os.path.join(project, "mutated_src_lines")
    out_dir = os.path.join(project, "injected_mutants")

    mutations = load_mutations(csv_dir, source_file, line_number)
    if not mutations:
        print("No mutations found matching the given filters")
        sys.exit(1)

    source_cache = {}
    os.makedirs(out_dir, exist_ok=True)
    total = 0
    invalid = 0

    for row in mutations:
        src_path_key = row["source_file"]
        filename = os.path.basename(src_path_key)
        lineno = int(row["line_number"])

        if src_path_key not in source_cache:
            abs_path = os.path.join(src_root, src_path_key.replace("/", os.sep))
            if not os.path.exists(abs_path):
                abs_path = find_source(src_root, filename)
            if not abs_path or not os.path.exists(abs_path):
                continue
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                source_cache[src_path_key] = f.readlines()

        original_lines = source_cache[src_path_key]
        mutant_lines = inject_mutation(original_lines, lineno, row["mutated_line"])
        mutant_text = "".join(mutant_lines)

        base_name = os.path.splitext(filename)[0]
        total += 1
        out_path = os.path.join(out_dir, f"{base_name}_line{lineno}_mutant{total}.java")

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(mutant_text)

        valid = validate_syntax(mutant_text)
        if not valid:
            invalid += 1
        print(f"  {os.path.basename(out_path)}: {row['description']}" + (" [INVALID]" if not valid else ""))

    print(f"\nWrote {total} mutants to {out_dir}")
    if invalid:
        print(f"  {invalid} failed tokenization")


if __name__ == "__main__":
    main()