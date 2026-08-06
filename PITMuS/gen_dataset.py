#!/usr/bin/env python3
"""CLI: PIT report -> dataset CSVs.

Thin front-end. All reconstruction logic lives in the `shared` package,
shared with inject.py; this file only walks mutations.xml and writes CSVs.
"""

# Run from a bare clone without installing: put this folder on sys.path so
# `import shared` resolves (the package lives in the sibling `shared/` dir).
# Harmless when the package is pip-installed.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import csv
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

from shared import *
from shared import dataset_dirname


DATASET = dataset_dirname()


def iter_mutations(system_path, skips=None):
    """Yield mutation records (with method-body context) for the dataset.

    Every mutation that is *not* reconstructed is appended to ``skips`` (if a
    list is given) as an ``(xml_line, location, reason)`` tuple, so the caller
    can record which mutations were dropped and why.
    """
    xml_path = os.path.join(system_path, "target", "pit-reports", "mutations.xml")
    src_root = os.path.join(system_path, "src", "main", "java")
    extra_src_roots = [
        os.path.join(system_path, "target", "generated-sources", "java"),
        os.path.join(system_path, "target", "generated-sources", "jjtree"),
        os.path.join(system_path, "target", "generated-sources", "annotations"),
    ]
    classes_root = os.path.join(system_path, "target", "classes")

    switch_table_cache = {}

    def get_switch_tables(fqcn):
        if fqcn in switch_table_cache:
            return switch_table_cache[fqcn]
        class_file = os.path.join(classes_root, fqcn.replace(".", os.sep) + ".class")
        switch_table_cache[fqcn] = load_switch_tables(class_file)
        return switch_table_cache[fqcn]

    tree = ET.parse(xml_path)
    root = tree.getroot()

    dbg_skips = os.environ.get("PITMUS_DEBUG_SKIPS")
    # Physical line of each <mutation> in mutations.xml, so every dataset row can be
    # traced back to the exact line in the XML file (assumes one <mutation> per line).
    xml_line_of = {}
    muts_in_order = root.findall("mutation")
    phys_lines = []
    with open(xml_path, encoding="utf-8", errors="replace") as _f:
        for _i, _ln in enumerate(_f, 1):
            # match a <mutation> start tag only, not the root <mutations> element
            if re.search(r"<mutation[ >]", _ln):
                phys_lines.append(_i)
    for _m, _pl in zip(muts_in_order, phys_lines):
        xml_line_of[id(_m)] = _pl

    def emit_skip(mut, rel_src, lineno, desc, bc_index, reason):
        xml_line = xml_line_of.get(id(mut), "?")
        row = f"({rel_src}:{lineno}) mutator={mut.findtext('mutator','')} index={bc_index} desc={desc!r}"
        if skips is not None:
            skips.append((xml_line, row, reason))
        if dbg_skips:
            print(f"{xml_line}\t{row}\treason: {reason}", file=sys.stderr)

    by_file = defaultdict(list)
    for mut in root.findall("mutation"):
        by_file[mut.findtext("sourceFile", "")].append(mut)

    source_cache = {}
    index_no = 0

    for src_file, mutations in by_file.items():
        mutations.sort(key=_mutation_sort_key)
        occ_counter = defaultdict(int)

        for mut in mutations:
            cls = mut.findtext("mutatedClass", "")
            method = mut.findtext("mutatedMethod", "")
            method_desc = mut.findtext("methodDescription", "")
            lineno = int(mut.findtext("lineNumber", "0"))
            desc = mut.findtext("description", "")
            killing = mut.findtext("killingTests", "") or ""
            covering = mut.findtext("coveringTests", "") or ""
            index_vals = [int(x.text) for x in mut.findall("indexes/index") if (x.text or "").lstrip("-").isdigit()]
            bc_index = index_vals[0] if index_vals else None

            pkg = cls.rsplit(".", 1)[0] if "." in cls else ""
            rel_src = os.path.join(pkg.replace(".", "/"), src_file)
            abs_path = os.path.join(src_root, pkg.replace(".", os.sep), src_file)
            if not os.path.isfile(abs_path):
                for alt_root in extra_src_roots:
                    candidate = os.path.join(alt_root, pkg.replace(".", os.sep), src_file)
                    if os.path.isfile(candidate):
                        abs_path = candidate
                        break

            if abs_path not in source_cache:
                source_cache[abs_path] = load_source(abs_path)
            lines, tokens, spans = source_cache[abs_path]

            # Occurrence rank = positional count of same-family mutations in the
            # order PIT emits them, which follows PIT's ASM instruction ordinal
            # (== source order for straight-line multi-condition expressions).
            # NOTE: do NOT resolve occurrence via javap byte offsets: PIT's
            # <index> is an ASM instruction ordinal, not a byte offset. Comparing
            # the two number spaces produces wrong occurrences on coincidental
            # numeric collisions (verified against +EXPORT ground truth for
            # PUSH.java:138 Integer||Short||Byte — index 15 is Byte, not Short).
            key = (src_file, method, method_desc, lineno, desc)
            occ = occ_counter[key]
            occ_counter[key] += 1

            if not (0 < lineno <= len(lines)):
                emit_skip(mut, rel_src, lineno, desc, bc_index, "lineNumber out of source-file range")
                continue
            sw_result = None
            if "Changed switch default" in desc:
                sw_result = reconstruct_switch_default(
                    lines, lineno, get_switch_tables(cls),
                )
            if sw_result is not None:
                stmt_start, stmt_end, raw_mutated_text = sw_result
            else:
                stmt_start, stmt_end, raw_mutated_text = apply_mutation_with_fallback(
                    lines, tokens, lineno, desc, occ, spans,
                )
            if not (0 < stmt_start <= len(lines)) or not (0 < stmt_end <= len(lines)):
                emit_skip(mut, rel_src, lineno, desc, bc_index, "statement bounds out of range (could not locate statement)")
                continue
            orig = "\n".join(lines[stmt_start - 1:stmt_end]).strip()
            mutated = raw_mutated_text.strip()
            if mutated.endswith("// MUTATED: " + desc):
                emit_skip(mut, rel_src, lineno, desc, bc_index, "mutation not applied (fallback marker; mutator/pattern not matched on line)")
                continue
            if orig == mutated:
                emit_skip(mut, rel_src, lineno, desc, bc_index, "no textual change (orig == mutated)")
                continue

            span = find_span_for_line(spans, stmt_start) if spans else None
            if not span:
                # Mutation lives outside any method body (e.g. a field
                # initializer or a lambda assigned to a field). Use the
                # statement itself as the enclosing unit so it isn't dropped.
                span = (stmt_start, stmt_end, "<field>")
            s, e, _name = span
            body_lines = lines[s - 1:e]
            original_method = "\n".join(body_lines)
            mutated_body = list(body_lines)
            mutated_body[stmt_start - s:stmt_end - s + 1] = raw_mutated_text.split("\n")
            mutated_method = "\n".join(mutated_body)
            if not original_method.strip() or original_method == mutated_method:
                emit_skip(mut, rel_src, lineno, desc, bc_index, "empty or unchanged method body after mutation")
                continue
            docstring = extract_javadoc(lines, s)

            index_no += 1
            yield {
                "index_no": index_no,
                "stmt_start_line": stmt_start,       # first line of the enclosing statement span
                "pit_line_number": lineno,           # <lineNumber> as recorded by PIT in mutations.xml
                "xml_line": xml_line_of.get(id(mut), ""),  # physical line of this <mutation> in mutations.xml
                "original_line": orig,
                "mutated_line": mutated,
                "source_file": rel_src,
                "description": desc,
                "test_files": extract_test_files(covering or killing),
                "original_method": original_method,
                "mutated_method_body": mutated_method,
                "docstring": docstring,
            }


def main():
    if len(sys.argv) < 2:
        print("Usage: python gen_dataset.py <system_path>")
        sys.exit(1)

    system_path = os.path.abspath(sys.argv[1].rstrip("/"))
    if not os.path.isdir(system_path):
        print(f"Error: {system_path} is not a directory")
        sys.exit(1)

    xml_path = os.path.join(system_path, "target", "pit-reports", "mutations.xml")
    if not os.path.isfile(xml_path):
        print(f"Error: {xml_path} not found")
        sys.exit(1)

    dataset_dir = os.path.join(system_path, DATASET)
    os.makedirs(dataset_dir, exist_ok=True)

    methods_rows = []
    meta_rows = []
    skips = []
    for info in iter_mutations(system_path, skips):
        methods_rows.append([
            info["index_no"],
            info["original_method"],
            info["mutated_method_body"],
            info["docstring"],
        ])
        meta_rows.append([
            info["original_line"],
            info["mutated_line"],
            info["source_file"],
            info["stmt_start_line"],
            info["pit_line_number"],
            info["description"],
            info["test_files"],
            info["index_no"],
            info["xml_line"],
        ])

    project = os.path.basename(system_path)
    methods_csv = os.path.join(dataset_dir, f"mutated_methods-{project}.csv")
    meta_csv = os.path.join(dataset_dir, f"meta-{project}.csv")
    with open(methods_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_NONNUMERIC)
        w.writerow(["index_no", "original_method", "mutated_method", "docstring"])
        w.writerows(methods_rows)
    with open(meta_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_NONNUMERIC)
        w.writerow([
            "mutation_line", "mutated_line", "source_file",
            "stmt_start_line", "pit_line_number", "description", "test_file", "index_no", "xml_line",
        ])
        w.writerows(meta_rows)

    # record which mutations were NOT reconstructed, and why -> evaluation_results
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    results_dir = os.path.join(repo_root, "evaluation", "evaluation_results", f"{project}_results")
    os.makedirs(results_dir, exist_ok=True)
    skips_txt = os.path.join(results_dir, f"skipped-reconstructions-samples-{project}.txt")
    skips.sort(key=lambda s: (isinstance(s[0], str), s[0]))
    with open(skips_txt, "w", encoding="utf-8") as f:
        f.write(f"# {project}: {len(skips)} of {len(methods_rows) + len(skips)} "
                f"mutation(s) skipped (not reconstructed)\n")
        f.write("# xml_line <TAB> (source_file:line) mutator=.. index=.. desc=.. <TAB> reason\n")
        for xml_line, row, reason in skips:
            f.write(f"{xml_line}\t{row}\treason: {reason}\n")

    print(f"Wrote {methods_csv} and {meta_csv} ({len(methods_rows)} rows)")
    print(f"Wrote {skips_txt} ({len(skips)} skipped)")


if __name__ == "__main__":
    main()