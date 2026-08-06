#!/usr/bin/env python3
"""CLI: PIT report -> mutant .java files.

Thin front-end. All reconstruction logic lives in the `pitmus` package,
shared with gen_dataset.py; this file adds bytecode-based occurrence
resolution, CLI target selection, and .java emission.
"""

# Run from a bare clone without installing: put the repo root on sys.path so
# `import pitmus` resolves. Harmless when the package is pip-installed.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

import javalang
from javalang.tokenizer import LexerError

from pitmus import *
from pitmus import DATASET_VERSION, dataset_dirname


USAGE = """\
Usage:
  python inject.py <system_path>                              # whole system
  python inject.py <system_path> line  <class.method:line>    # one line in one method
  python inject.py <system_path> id    <index_no>             # one mutation by id
  python inject.py <system_path> file  <class_fqn|file.java>  # all mutations in one file

Examples:
  python inject.py test-projects/joda-time
  python inject.py test-projects/joda-time line org.joda.time.DateTime.plus:614
  python inject.py test-projects/joda-time id 614
  python inject.py test-projects/joda-time file org.joda.time.DateTime
"""


def family_for_desc(desc):
    d = desc.strip()
    m = re.match(r"Replaced (?:integer|long|float|double) (\w+) with", d)
    if m:
        return MATH_OPCODES.get(m.group(1))
    if d == "Replaced Shift Left with Shift Right":        return {"ishl", "lshl"}
    if d == "Replaced Shift Right with Shift Left":        return {"ishr", "lshr"}
    if d == "Replaced Unsigned Shift Right with Shift Left": return {"iushr", "lushr"}
    if d == "Replaced XOR with AND":                       return {"ixor", "lxor"}
    if d == "Replaced bitwise AND with OR":                return {"iand", "land"}
    if d == "Replaced bitwise OR with AND":                return {"ior", "lor"}
    if d == "changed conditional boundary":                return COND_BOUNDARY_OPCODES
    if d == "negated conditional":                         return COND_OPCODES
    if d.startswith("removed conditional"):                return COND_OPCODES
    if d == "removed negation":                            return {"ineg", "lneg", "fneg", "dneg"}
    if d.startswith("Changed increment"):                  return {"iinc"}
    if d.startswith("removed call to"):                    return INVOKE_OPCODES
    if re.search(r"replaced .*return.*with", d, re.IGNORECASE): return RETURN_OPCODES
    if "Changed switch default" in d:                      return {"tableswitch", "lookupswitch"}
    return None


def _parse_javap(text, simple_class_name):
    methods = {}
    lines = text.splitlines()
    current = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("descriptor:") and i > 0:
            sig = lines[i - 1].strip().rstrip(";").rstrip()
            mm = re.search(r"([\w<>$]+)\s*\([^)]*\)\s*$", sig)
            name = None
            if mm:
                name = mm.group(1)
                if name == simple_class_name:
                    name = "<init>"
            elif "static" in sig and "{}" in sig:
                name = "<clinit>"
            if name:
                desc = stripped[len("descriptor:"):].strip()
                current = {"insns": [], "lnt": []}
                methods[(name, desc)] = current
            else:
                current = None
            continue
        if current is None:
            continue
        cm = re.match(r"\s+(\d+):\s+(\w+)", line)
        if cm:
            current["insns"].append((int(cm.group(1)), cm.group(2).lower()))
            continue
        lm = re.match(r"\s+line\s+(\d+):\s+(\d+)\s*$", line)
        if lm:
            current["lnt"].append((int(lm.group(2)), int(lm.group(1))))
    return methods


def load_class_bytecode(class_file):
    if not os.path.exists(class_file):
        return {}
    try:
        out = subprocess.run(
            ["javap", "-c", "-p", "-l", class_file],
            capture_output=True, text=True, timeout=60,
        )
        text = out.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return {}
    simple = os.path.splitext(os.path.basename(class_file))[0]
    simple = simple.rsplit("$", 1)[-1]
    return _parse_javap(text, simple)


def resolve_occ_from_bytecode(methods_info, method, method_desc, index, line, desc):
    info = methods_info.get((method, method_desc))
    if not info or not info["insns"]:
        return None
    family = family_for_desc(desc)
    if not family:
        return None
    lnt = sorted(info["lnt"])
    matches_on_line = []
    for off, mnem in info["insns"]:
        if mnem not in family:
            continue
        if _line_for_offset(lnt, off) != line:
            continue
        matches_on_line.append(off)
    if index not in matches_on_line:
        return None
    return matches_on_line.index(index)


def iter_mutations(system_path):
    """Yield (index_no, info) for each valid mutation. Same order/filters as gen_dataset.py."""
    xml_path = os.path.join(system_path, "target", "pit-reports", "mutations.xml")
    src_root = os.path.join(system_path, "src", "main", "java")
    extra_src_roots = [
        os.path.join(system_path, "target", "generated-sources", "java"),
        os.path.join(system_path, "target", "generated-sources", "jjtree"),
        os.path.join(system_path, "target", "generated-sources", "annotations"),
    ]
    classes_root = os.path.join(system_path, "target", "classes")

    bytecode_cache = {}

    def get_bytecode(fqcn):
        if fqcn in bytecode_cache:
            return bytecode_cache[fqcn]
        class_file = os.path.join(classes_root, fqcn.replace(".", os.sep) + ".class")
        bytecode_cache[fqcn] = load_class_bytecode(class_file)
        return bytecode_cache[fqcn]

    tree = ET.parse(xml_path)
    root = tree.getroot()

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

            key = (src_file, method, method_desc, lineno, desc)
            bc_occ = None
            if bc_index is not None:
                bc_occ = resolve_occ_from_bytecode(
                    get_bytecode(cls), method, method_desc, bc_index, lineno, desc,
                )
            occ = bc_occ if bc_occ is not None else occ_counter[key]
            occ_counter[key] += 1

            if not (0 < lineno <= len(lines)):
                continue
            stmt_start, stmt_end, raw_mutated_text = apply_mutation_with_fallback(
                lines, tokens, lineno, desc, occ,
            )
            if not (0 < stmt_start <= len(lines)) or not (0 < stmt_end <= len(lines)):
                continue
            orig = "\n".join(lines[stmt_start - 1:stmt_end]).strip()
            mutated = raw_mutated_text.strip()
            if mutated.endswith("// MUTATED: " + desc):
                continue
            if orig == mutated:
                continue

            span = find_span_for_line(spans, stmt_start) if spans else None
            if not span:
                continue
            s, e, _name = span
            body_lines = lines[s - 1:e]
            mutated_body = list(body_lines)
            mutated_body[stmt_start - s:stmt_end - s + 1] = raw_mutated_text.split("\n")
            if "\n".join(body_lines).strip() == "" or body_lines == mutated_body:
                continue

            index_no += 1
            yield {
                "index_no": index_no,
                "mutated_class": cls,
                "mutated_method": method,
                "line_number": stmt_start,
                "stmt_start": stmt_start,
                "stmt_end": stmt_end,
                "raw_mutated_line": raw_mutated_text,
                "source_file": rel_src,
                "source_lines": lines,
                "description": desc,
            }


def inject_at(source_lines, stmt_start, stmt_end, mutated_text):
    """Replace lines [stmt_start..stmt_end] (1-based, inclusive) with `mutated_text`.
    Preserves the indentation of the first line; `mutated_text` may contain newlines."""
    result = [ln + "\n" for ln in source_lines]
    if not (1 <= stmt_start <= len(result)) or not (stmt_start <= stmt_end <= len(result)):
        return "".join(result)
    first_line = result[stmt_start - 1]
    indent = " " * (len(first_line) - len(first_line.lstrip()))
    new_lines = mutated_text.split("\n")
    new_lines[0] = indent + new_lines[0].lstrip()
    new_chunk = "\n".join(new_lines) + "\n"
    result[stmt_start - 1:stmt_end] = [new_chunk]
    return "".join(result)


def validate_syntax(source_text):
    try:
        list(javalang.tokenizer.tokenize(source_text))
        return True
    except (LexerError, StopIteration, Exception):
        return False


def parse_line_target(target):
    if ":" not in target:
        raise ValueError(f"line target must be <class.method:line>, got {target!r}")
    head, line_s = target.rsplit(":", 1)
    if "." not in head:
        raise ValueError(f"line target must be <class.method:line>, got {target!r}")
    cls, method = head.rsplit(".", 1)
    return cls, method, int(line_s)


def file_target_to_rel(target):
    if target.endswith(".java"):
        return target
    return target.replace(".", "/") + ".java"


def matches(info, mode, target):
    if mode is None:
        return True
    if mode == "id":
        return info["index_no"] == int(target)
    if mode == "line":
        cls, method, line = parse_line_target(target)
        return (
            info["mutated_class"] == cls
            and info["mutated_method"] == method
            and info["line_number"] == line
        )
    if mode == "file":
        rel = file_target_to_rel(target)
        return (
            info["source_file"] == rel
            or info["source_file"].endswith("/" + rel)
            or info["source_file"] == os.path.basename(rel)
        )
    raise ValueError(f"unknown mode: {mode}")


def write_mutant(out_dir, info, counter):
    mutant_text = inject_at(info["source_lines"], info["stmt_start"], info["stmt_end"], info["raw_mutated_line"])
    base = os.path.splitext(os.path.basename(info["source_file"]))[0]
    out_name = f"{base}_id{info['index_no']}_line{info['line_number']}.java"
    out_path = os.path.join(out_dir, out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(mutant_text)
    valid = validate_syntax(mutant_text)
    flag = " [INVALID]" if not valid else ""
    print(f"  [{counter}] {out_name}: {info['description']}{flag}")
    return valid


def main():
    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(1)

    system_path = os.path.abspath(sys.argv[1].rstrip("/"))
    if not os.path.isdir(system_path):
        print(f"Error: {system_path} is not a directory")
        sys.exit(1)

    mode = None
    target = None
    if len(sys.argv) >= 3:
        mode = sys.argv[2].strip().lower()
        if mode not in ("line", "id", "file"):
            print(f"Error: unknown mode {mode!r}. Use 'line', 'id', or 'file'.")
            print(USAGE)
            sys.exit(1)
        if len(sys.argv) < 4:
            print(f"Error: mode '{mode}' requires a target argument")
            print(USAGE)
            sys.exit(1)
        target = sys.argv[3]

    xml_path = os.path.join(system_path, "target", "pit-reports", "mutations.xml")
    if not os.path.isfile(xml_path):
        print(f"Error: {xml_path} not found")
        sys.exit(1)

    out_dir = os.path.join(system_path, "injected_mutants")
    os.makedirs(out_dir, exist_ok=True)

    total = 0
    invalid = 0
    for info in iter_mutations(system_path):
        try:
            keep = matches(info, mode, target)
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        if not keep:
            continue
        total += 1
        if not write_mutant(out_dir, info, total):
            invalid += 1
        if mode == "id":
            break

    if total == 0:
        print("No mutations matched the given filter")
        sys.exit(1)

    print(f"\nWrote {total} mutants to {out_dir}")
    if invalid:
        print(f"  {invalid} failed tokenization")


if __name__ == "__main__":
    main()