import xml.etree.ElementTree as ET
import csv
import os
import sys
import re
import subprocess
from collections import defaultdict

import javalang
from javalang.tokenizer import LexerError


MATH_OPCODES = {
    "addition":       {"iadd", "ladd", "fadd", "dadd"},
    "subtraction":    {"isub", "lsub", "fsub", "dsub"},
    "multiplication": {"imul", "lmul", "fmul", "dmul"},
    "division":       {"idiv", "ldiv", "fdiv", "ddiv"},
    "modulus":        {"irem", "lrem", "frem", "drem"},
}
COND_OPCODES = {
    "ifeq", "ifne", "iflt", "ifle", "ifgt", "ifge",
    "if_icmpeq", "if_icmpne", "if_icmplt", "if_icmple", "if_icmpgt", "if_icmpge",
    "if_acmpeq", "if_acmpne", "ifnull", "ifnonnull",
}
COND_BOUNDARY_OPCODES = {
    "iflt", "ifle", "ifgt", "ifge",
    "if_icmplt", "if_icmple", "if_icmpgt", "if_icmpge",
}
RETURN_OPCODES = {"ireturn", "lreturn", "freturn", "dreturn", "areturn"}
INVOKE_OPCODES = {"invokevirtual", "invokestatic", "invokeinterface", "invokespecial", "invokedynamic"}


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


def _line_for_offset(lnt_sorted, offset):
    result = None
    for off, ln in lnt_sorted:
        if off <= offset:
            result = ln
        else:
            break
    return result


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
        name = m.group(1)
        indent = line[: len(line) - len(line.lstrip())]
        call_re = re.compile(r'(?:\b\w+\s*\.\s*)*\b' + re.escape(name) + r'\s*\([^()]*\)\s*;?')
        stripped = call_re.sub('', line)
        if stripped.strip():
            return stripped.rstrip() + "  // removed call to " + name + "()"
        return indent + "// removed call to " + name + "()"

    m = re.match(r"replaced (?:\w+ )?return value with (.+)", d) or \
        re.match(r"replaced (?:boolean|Boolean) return with (.+)", d) or \
        re.match(r"replaced (?:int|long|short|byte|char|float|double|Integer|Long|Short|Double|Float|Character|Boolean) return.*with (\S+)", d)
    if m:
        val = re.sub(r'\s+for\s+\S+::\S+$', '', m.group(1)).strip()
        if val == "&quot;&quot;" or val == '""':
            val = '""'
        elif val == "True":
            val = "Boolean.TRUE"
        elif val == "False":
            val = "Boolean.FALSE"
        elif "." in val and not val.endswith(")") and not val.endswith(";"):
            val += "()"
        result = re.sub(r'return\s+.+;', 'return ' + val + ';', line, count=1)
        if result != line:
            return result
        stripped = line.strip()
        if stripped == 'return' or stripped.startswith('return ') or stripped.startswith('return\t'):
            indent = line[: len(line) - len(line.lstrip())]
            return indent + 'return ' + val + ';'
        if re.match(r'\s*(?:public|private|protected|static|final|\w[\w<>,\[\]\s]*\s+\w+\s*\()', line):
            return line + " // return value replaced with " + val

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
    classes_root = os.path.join(project, "target", "classes")
    out_dir = os.path.join(project, "mutated_src_lines")
    os.makedirs(out_dir, exist_ok=True)

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
    total = 0

    for src_file, mutations in by_file.items():
        def _sort_key(m):
            blocks_vals = [int(b.text) for b in m.findall("blocks/block") if (b.text or "").lstrip("-").isdigit()]
            idx_vals = [int(x.text) for x in m.findall("indexes/index") if (x.text or "").lstrip("-").isdigit()]
            first_block = blocks_vals[0] if blocks_vals else 0
            first_idx = idx_vals[0] if idx_vals else 0
            return (
                m.findtext("mutatedClass", ""),
                m.findtext("mutatedMethod", ""),
                m.findtext("methodDescription", ""),
                int(m.findtext("lineNumber", "0") or 0),
                first_block,
                first_idx,
            )
        mutations.sort(key=_sort_key)

        occ_counter = defaultdict(int)
        csv_name = os.path.splitext(src_file)[0] + ".csv"
        csv_path = os.path.join(out_dir, csv_name)
        rows = []

        for mut in mutations:
            cls = mut.findtext("mutatedClass", "")
            method = mut.findtext("mutatedMethod", "")
            method_desc = mut.findtext("methodDescription", "")
            lineno = int(mut.findtext("lineNumber", "0"))
            desc = mut.findtext("description", "")
            killing = mut.findtext("killingTests", "") or ""
            covering = mut.findtext("coveringTests", "") or ""
            blocks = "|".join(b.text or "" for b in mut.findall("blocks/block"))
            index_vals = [int(x.text) for x in mut.findall("indexes/index") if (x.text or "").lstrip("-").isdigit()]
            bc_index = index_vals[0] if index_vals else None

            pkg = cls.rsplit(".", 1)[0] if "." in cls else ""
            rel_src = os.path.join(pkg.replace(".", "/"), src_file)
            abs_path = os.path.join(src_root, pkg.replace(".", os.sep), src_file)

            if abs_path not in source_cache:
                source_cache[abs_path] = load_source(abs_path)
            lines, tokens = source_cache[abs_path]

            key = (src_file, method, method_desc, lineno, desc)
            bc_occ = None
            if bc_index is not None:
                bc_occ = resolve_occ_from_bytecode(
                    get_bytecode(cls), method, method_desc, bc_index, lineno, desc,
                )
            if bc_occ is not None:
                occ = bc_occ
            else:
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
                orig, mutated, rel_src, lineno, desc, test_files, blocks
            ])

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, quoting=csv.QUOTE_NONNUMERIC)
            w.writerow([
                "mutation_line", "mutated_line", "source_file",
                "line_number", "description", "test_file", "block"
            ])
            w.writerows(rows)

        total += len(rows)
        print(f"{csv_path}: {len(rows)} mutations")

    print(f"Total: {total} mutations across {len(by_file)} files")


if __name__ == "__main__":
    main()
