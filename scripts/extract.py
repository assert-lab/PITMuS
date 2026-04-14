import xml.etree.ElementTree as ET
import csv
import os
import sys
import re
from collections import defaultdict

import javalang
from javalang.tokenizer import LexerError


def load_source(path):
    if not os.path.exists(path):
        return [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        source = f.read()
    lines = source.splitlines()
    try:
        tokens = list(javalang.tokenizer.tokenize(source))
    except (LexerError, StopIteration, Exception):
        tokens = []
    return lines, tokens


def tokens_on_line(all_tokens, lineno):
    return [t for t in all_tokens if t.position and t.position[0] == lineno]


def replace_at(line, col0, old_len, replacement):
    return line[:col0] + replacement + line[col0 + old_len:]


def nth_token(toks, value, n):
    count = 0
    for t in toks:
        if t.value == value:
            if count == n:
                return t
            count += 1
    return None


def nth_token_in(toks, values, n):
    count = 0
    for t in toks:
        if t.value in values:
            if count == n:
                return t
            count += 1
    return None


def _expr_end_col(ltoks, start_idx):
    depth = 0
    last_i = start_idx
    for i in range(start_idx, len(ltoks)):
        v = ltoks[i].value
        if v in ('(', '['):
            depth += 1
        elif v in (')', ']'):
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and v in ('||', '&&', ';', '{', ',', '?', ':'):
            break
        last_i = i
    t = ltoks[last_i]
    return t.position[1] - 1 + len(t.value)


def _expr_start_col(ltoks, end_idx):
    depth = 0
    first_i = end_idx
    for i in range(end_idx, -1, -1):
        v = ltoks[i].value
        if v in (')', ']'):
            depth += 1
        elif v in ('(', '['):
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and v in ('||', '&&', ';', '{', ',', '?', ':', 'return', 'throw', '=', '==', '!='):
            break
        first_i = i
    t = ltoks[first_i]
    return t.position[1] - 1


def _find_eq_method_calls(ltoks):
    calls = []
    for i, tok in enumerate(ltoks):
        if tok.value in ('equals', 'equalsIgnoreCase') and i >= 2 and ltoks[i - 1].value == '.':
            obj_start = _expr_start_col(ltoks, i - 2)
            if i + 1 < len(ltoks) and ltoks[i + 1].value == '(':
                call_end = _expr_end_col(ltoks, i + 1)
                calls.append((obj_start, call_end))
    return calls


def _find_compound_bool_subexprs(ltoks):
    results = []
    n = len(ltoks)
    for i, tok in enumerate(ltoks):
        if tok.value not in ('&&', '||'):
            continue
        j = i + 1
        while j < n and ltoks[j].value == '!':
            j += 1
        if j >= n:
            continue
        if ltoks[j].value in ('(', ')', ';', '{', '}', ',', '&&', '||'):
            continue
        start_col = ltoks[j].position[1] - 1
        end_col = _expr_end_col(ltoks, j)
        has_method_call = any(
            ltoks[k].value == '(' and k > j and ltoks[k - 1].value not in ('&&', '||', '(', '!', ';', '{')
            for k in range(j, n)
            if ltoks[k].position and ltoks[k].position[1] - 1 < end_col
        )
        if has_method_call:
            results.append((start_col, end_col))
    return results


def apply_mutation(line, ltoks, desc, occ=0):
    d = desc.strip()

    m = re.match(r"Replaced (?:integer|long|float|double) (\w+) with (\w+)", d)
    if m:
        ops = {
            "addition": "+", "subtraction": "-", "multiplication": "*",
            "division": "/", "modulus": "%",
        }
        old, new = ops.get(m.group(1)), ops.get(m.group(2))
        if old and new:
            t = nth_token(ltoks, old, occ)
            if t:
                return replace_at(line, t.position[1] - 1, len(old), new)

    shift_map = {
        "Replaced Shift Left with Shift Right": ("<<", ">>"),
        "Replaced Shift Right with Shift Left": (">>", "<<"),
        "Replaced Unsigned Shift Right with Shift Left": (">>>", "<<"),
        "Replaced XOR with AND": ("^", "&"),
        "Replaced bitwise AND with OR": ("&", "|"),
        "Replaced bitwise OR with AND": ("|", "&"),
    }
    if d in shift_map:
        old, new = shift_map[d]
        t = nth_token(ltoks, old, occ)
        if t:
            return replace_at(line, t.position[1] - 1, len(old), new)

    if d == "changed conditional boundary":
        bmap = {">=": ">", "<=": "<", ">": ">=", "<": "<="}
        t = nth_token_in(ltoks, bmap.keys(), occ)
        if t:
            return replace_at(line, t.position[1] - 1, len(t.value), bmap[t.value])

    if d == "negated conditional":
        nmap = {"==": "!=", "!=": "==", ">=": "<", "<=": ">", ">": "<=", "<": ">="}
        t = nth_token_in(ltoks, nmap.keys(), occ)
        if t:
            return replace_at(line, t.position[1] - 1, len(t.value), nmap[t.value])

    if d.startswith("removed conditional"):
        val = "true" if "with true" in d else "false"
        is_equality = "equality" in d
        comp_ops = {"==", "!="} if is_equality else {">", "<", ">=", "<="}

        candidates = []

        for i_tok, tok in enumerate(ltoks):
            if tok.value in comp_ops and 0 < i_tok < len(ltoks) - 1:
                candidates.append((
                    _expr_start_col(ltoks, i_tok - 1),
                    _expr_end_col(ltoks, i_tok + 1),
                ))

        if is_equality:
            for s, e in _find_eq_method_calls(ltoks):
                candidates.append((s, e))

            for m2 in re.finditer(
                    r'(?<!\w)(\w[\w.]*(?:\(\))?)\s+instanceof\s+(\w[\w.]*)', line):
                candidates.append((m2.start(), m2.end()))

            covered = {s for s, _ in candidates}
            for s, e in _find_compound_bool_subexprs(ltoks):
                if s not in covered:
                    candidates.append((s, e))
                    covered.add(s)

        for i_tok, tok in enumerate(ltoks):
            if tok.value == '?' and i_tok > 0:
                prev = ltoks[i_tok - 1]
                end_col = prev.position[1] - 1 + len(prev.value)
                start_col = _expr_start_col(ltoks, i_tok - 1)
                if not any(start_col <= s and e <= end_col for s, e in candidates):
                    candidates.append((start_col, end_col))

        candidates.sort(key=lambda x: x[0])
        if occ < len(candidates):
            s, e = candidates[occ]
            return line[:s] + val + line[e:]

        m2 = re.search(r'(?:if|while)\s*\((.+)\)', line)
        if m2:
            return line[:m2.start(1)] + val + line[m2.end(1):]
        m2 = re.search(r'\breturn\s+(.+);', line)
        if m2:
            return line[:m2.start(1)] + val + line[m2.end(1):]
        m2 = re.search(r'(\S+\([^)]*\))\s*\?', line)
        if m2:
            return line[:m2.start(1)] + val + line[m2.end(1):]

    if d == "removed negation":
        t = nth_token(ltoks, "-", occ)
        if t:
            return replace_at(line, t.position[1] - 1, 1, "")

    m = re.match(r"Changed increment from (-?\d+) to (-?\d+)", d)
    if m:
        old_v, new_v = int(m.group(1)), int(m.group(2))
        if old_v == 1 and new_v == -1:
            t = nth_token(ltoks, "++", occ)
            if t:
                return replace_at(line, t.position[1] - 1, 2, "--")
        elif old_v == -1 and new_v == 1:
            t = nth_token(ltoks, "--", occ)
            if t:
                return replace_at(line, t.position[1] - 1, 2, "++")
        else:
            abs_old = str(abs(old_v))
            for i, t in enumerate(ltoks):
                if t.value == abs_old:
                    col = t.position[1] - 1
                    prev_val = ltoks[i - 1].value if i > 0 else ""
                    if old_v < 0 and prev_val == "-=":
                        return replace_at(line, ltoks[i - 1].position[1] - 1, 2, "+=")
                    elif old_v > 0 and prev_val == "+=":
                        return replace_at(line, ltoks[i - 1].position[1] - 1, 2, "-=")
                    elif old_v < 0 and prev_val == "-":
                        start = ltoks[i - 1].position[1] - 1
                    else:
                        start = col
                    end = col + len(abs_old)
                    return line[:start] + str(new_v) + line[end:]

    m = re.match(r"removed call to .+::(\w+)", d)
    if m:
        return "// removed call to " + m.group(1) + "()"

    m = re.match(r"replaced (?:\w+ )?return value with (.+)", d) or \
        re.match(r"replaced boolean return with (.+)", d) or \
        re.match(r"replaced (?:int|long|short|byte|char|float|double|Integer|Long|Short|Double|Float|Character|Boolean) return.*with (\S+)", d)
    if m:
        val = re.sub(r'\s+for\s+\S+::\S+$', '', m.group(1)).strip()
        if val == "&quot;&quot;" or val == '""':
            val = '""'
        elif "." in val and not val.endswith(")") and not val.endswith(";"):
            val += "()"
        result = re.sub(r'return\s+.+;', 'return ' + val + ';', line, count=1)
        if result != line:
            return result
        stripped = line.strip()
        if stripped == 'return' or stripped.startswith('return ') or stripped.startswith('return\t'):
            indent = line[: len(line) - len(line.lstrip())]
            return indent + 'return ' + val + ';'

    if "Changed switch default" in d:
        return line + " // switch default changed to first case"

    return line + " // MUTATED: " + d


def extract_test_files(test_str):
    if not test_str:
        return ""
    files = set()
    for entry in test_str.split("|"):
        m = re.search(r'\(([^)]+)\)', entry)
        if m:
            cls = m.group(1).rsplit(".", 1)[-1]
            files.add(cls + ".java")
    return "|".join(sorted(files))


def main():
    if len(sys.argv) < 2:
        print("Usage: python extract_mutations.py <project-name>")
        print("  Expects project at test-projects/<project-name>/")
        sys.exit(1)

    project = os.path.join("test-projects", sys.argv[1].rstrip("/"))
    xml_path = os.path.join(project, "target", "pit-reports", "mutations.xml")
    src_root = os.path.join(project, "src", "main", "java")
    out_dir = os.path.join(project, "mutated_src_lines")
    os.makedirs(out_dir, exist_ok=True)

    tree = ET.parse(xml_path)
    root = tree.getroot()

    by_file = defaultdict(list)
    for mut in root.findall("mutation"):
        by_file[mut.findtext("sourceFile", "")].append(mut)

    source_cache = {}
    total = 0

    for src_file, mutations in by_file.items():
        occ_counter = defaultdict(int)
        csv_name = os.path.splitext(src_file)[0] + ".csv"
        csv_path = os.path.join(out_dir, csv_name)
        rows = []

        for mut in mutations:
            cls = mut.findtext("mutatedClass", "")
            lineno = int(mut.findtext("lineNumber", "0"))
            desc = mut.findtext("description", "")
            killing = mut.findtext("killingTests", "") or ""
            covering = mut.findtext("coveringTests", "") or ""

            pkg = cls.rsplit(".", 1)[0] if "." in cls else ""
            rel_src = os.path.join(pkg.replace(".", "/"), src_file)
            abs_path = os.path.join(src_root, pkg.replace(".", os.sep), src_file)

            if abs_path not in source_cache:
                source_cache[abs_path] = load_source(abs_path)
            lines, tokens = source_cache[abs_path]

            key = (src_file, lineno, desc)
            occ = occ_counter[key]
            occ_counter[key] += 1

            if 0 < lineno <= len(lines):
                raw_line = lines[lineno - 1]
                orig = raw_line.strip()
                ltoks = tokens_on_line(tokens, lineno)
                mutated = apply_mutation(raw_line, ltoks, desc, occ).strip()
            else:
                orig = ""
                mutated = ""

            if mutated.endswith("// MUTATED: " + desc):
                continue

            test_files = extract_test_files(covering or killing)

            rows.append([
                orig, mutated, rel_src, lineno, desc, test_files
            ])

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, quoting=csv.QUOTE_NONNUMERIC)
            w.writerow([
                "mutation_line", "mutated_line", "source_file",
                "line_number", "description", "test_file"
            ])
            w.writerows(rows)

        total += len(rows)
        print(f"{csv_path}: {len(rows)} mutations")

    print(f"Total: {total} mutations across {len(by_file)} files")


if __name__ == "__main__":
    main()
