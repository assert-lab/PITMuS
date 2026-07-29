#injection (end-to-end: extracts mutations from XML + bytecode and injects them)

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

import javalang
from javalang.tokenizer import LexerError


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
        return [], [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        source = f.read()
    lines = source.splitlines()
    # Neutralize LF/CR Unicode escapes so javalang's line numbering matches
    # splitlines() and PIT's lineNumber. Same length keeps char offsets aligned.
    safe = re.sub(r'\\u(?:000[AaDd])', r'\\u0020', source)
    try:
        tokens = list(javalang.tokenizer.tokenize(safe))
    except (LexerError, StopIteration, Exception):
        tokens = []
    spans = extract_method_spans(safe, lines)
    return lines, tokens, spans


def _find_method_end(lines, start_line):
    depth = 0
    started = False
    in_str = in_char = in_block = False
    i = start_line - 1
    while i < len(lines):
        line = lines[i]
        in_line = False
        j = 0
        while j < len(line):
            c = line[j]
            nxt = line[j + 1] if j + 1 < len(line) else ""
            if in_line:
                break
            if in_block:
                if c == "*" and nxt == "/":
                    in_block = False
                    j += 2
                    continue
                j += 1
                continue
            if in_str:
                if c == "\\":
                    j += 2
                    continue
                if c == '"':
                    in_str = False
                j += 1
                continue
            if in_char:
                if c == "\\":
                    j += 2
                    continue
                if c == "'":
                    in_char = False
                j += 1
                continue
            if c == "/" and nxt == "/":
                break
            if c == "/" and nxt == "*":
                in_block = True
                j += 2
                continue
            if c == '"':
                in_str = True
            elif c == "'":
                in_char = True
            elif c == "{":
                depth += 1
                started = True
            elif c == "}":
                depth -= 1
                if started and depth == 0:
                    return i + 1
            j += 1
        i += 1
    return None


def _find_brace_open_before(lines, body_start):
    depth = 0
    for i in range(body_start - 1, -1, -1):
        line = re.sub(r'//.*$', '', lines[i])
        for c in reversed(line):
            if c == '}':
                depth += 1
            elif c == '{':
                if depth == 0:
                    return i + 1
                depth -= 1
    return None


def _first_body_line(node):
    if not getattr(node, "body", None):
        return None
    for item in node.body:
        pos = getattr(item, "position", None)
        if pos:
            return pos.line
    return None


def extract_method_spans(source, lines):
    try:
        tree = javalang.parse.parse(source)
    except Exception:
        return []
    spans = []
    seen = set()
    for path, node in tree.filter(javalang.tree.MethodDeclaration):
        if node.position and node.body is not None:
            start = node.position.line
            end = _find_method_end(lines, start)
            if end and (start, end) not in seen:
                spans.append((start, end, node.name))
                seen.add((start, end))
    for path, node in tree.filter(javalang.tree.ConstructorDeclaration):
        if node.position and node.body is not None:
            start = node.position.line
            end = _find_method_end(lines, start)
            if end and (start, end) not in seen:
                spans.append((start, end, node.name))
                seen.add((start, end))
    for path, node in tree.filter(javalang.tree.ClassCreator):
        if node.body is None:
            continue
        body_line = _first_body_line(node)
        if body_line is None:
            continue
        anon_start = _find_brace_open_before(lines, body_line)
        if not anon_start:
            continue
        end = _find_method_end(lines, anon_start)
        if end and (anon_start, end) not in seen:
            spans.append((anon_start, end, "<anon>"))
            seen.add((anon_start, end))
    if hasattr(javalang.tree, "LambdaExpression"):
        for path, node in tree.filter(javalang.tree.LambdaExpression):
            body = getattr(node, "body", None)
            first_line = None
            if isinstance(body, list):
                for stmt in body:
                    p = getattr(stmt, "position", None)
                    if p:
                        first_line = p.line; break
            elif body is not None:
                p = getattr(body, "position", None)
                if p:
                    first_line = p.line
            if first_line is None:
                for ancestor in reversed(path):
                    p = getattr(ancestor, "position", None)
                    if p:
                        first_line = p.line; break
            if first_line is None:
                continue
            if isinstance(body, list):
                start = _find_brace_open_before(lines, first_line) or first_line
                end = _find_method_end(lines, start) or first_line
            else:
                start = first_line
                end = first_line
            if (start, end) not in seen:
                spans.append((start, end, "<lambda>"))
                seen.add((start, end))
    spans.sort()
    return spans


def find_span_for_line(spans, lineno):
    best = None
    for s, e, name in spans:
        if s <= lineno <= e:
            if best is None or (e - s) < (best[1] - best[0]):
                best = (s, e, name)
    return best


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


def nth_gt_run(ltoks, count, occ):
    matches = 0
    i = 0
    while i + count <= len(ltoks):
        if all(ltoks[i + k].value == ">" for k in range(count)):
            cols = [ltoks[i + k].position[1] for k in range(count)]
            if all(cols[k] + 1 == cols[k + 1] for k in range(count - 1)):
                if matches == occ:
                    return ltoks[i]
                matches += 1
                i += count
                continue
        i += 1
    return None


def _generic_bracket_indices(toks):
    """Indices of '<'/'>' tokens that are Java generic type brackets rather than
    comparison/shift operators. A generic opener is a '<' preceded by a type name
    or a '.' type-witness that opens a balanced group whose contents are type-like
    (identifiers, '.', ',', '?', '[', ']', 'extends', 'super', nested '<'/'>').
    Used to keep operator selection aligned with the bytecode occurrence index,
    which counts only real comparison opcodes (generics produce none)."""
    generic = set()
    n = len(toks)
    type_like = {'.', ',', '?', '[', ']', 'extends', 'super'}
    for i, tok in enumerate(toks):
        if tok.value != '<' or i == 0:
            continue
        prev = toks[i - 1].value
        if not (prev == '.' or (prev.isidentifier() and prev[:1].isupper())):
            continue
        depth = 0
        j = i
        ok = True
        while j < n:
            v = toks[j].value
            if v == '<':
                depth += 1
            elif v == '>':
                depth -= 1
                if depth == 0:
                    break
            elif not (v.isidentifier() or v in type_like):
                ok = False
                break
            j += 1
        if ok and depth == 0 and j < n:
            for k in range(i, j + 1):
                if toks[k].value in ('<', '>'):
                    generic.add(k)
    return generic


def nth_token_in(toks, values, n, skip=None):
    skip = skip or set()
    count = 0
    for i, t in enumerate(toks):
        if i in skip:
            continue
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


def _find_any_method_calls(ltoks):
    calls = []
    n = len(ltoks)
    for i, tok in enumerate(ltoks):
        if tok.value != '(' or i == 0:
            continue
        name = ltoks[i - 1].value
        if not (name.isidentifier() and not name[0].isupper() and name not in ('if', 'while', 'for', 'switch', 'return', 'new', 'throw', 'catch')):
            continue
        start_idx = i - 1
        if start_idx >= 2 and ltoks[start_idx - 1].value == '.':
            start_idx = start_idx - 2
            while start_idx > 0 and ltoks[start_idx - 1].value == '.':
                start_idx -= 2
        start_col = ltoks[start_idx].position[1] - 1
        end_col = _expr_end_col(ltoks, i)
        if end_col > start_col:
            calls.append((start_col, end_col))
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
        k = i - 1
        while k >= 0 and ltoks[k].value in (')', ']'):
            depth = 1
            k -= 1
            while k >= 0 and depth > 0:
                if ltoks[k].value in (')', ']'):
                    depth += 1
                elif ltoks[k].value in ('(', '['):
                    depth -= 1
                k -= 1
        if k >= 0 and ltoks[k].value not in ('&&', '||', '(', '{', ',', ';', '?', ':'):
            end_lhs = ltoks[i - 1].position[1] - 1 + len(ltoks[i - 1].value)
            start_lhs = _expr_start_col(ltoks, i - 1)
            if start_lhs < end_lhs:
                results.append((start_lhs, end_lhs))
    return results


def _find_bool_assignment_rhs(ltoks):
    results = []
    n = len(ltoks)
    for i, tok in enumerate(ltoks):
        if tok.value != '=':
            continue
        if i > 0 and ltoks[i - 1].value in ('=', '!', '<', '>', '+', '-', '*', '/', '%', '&', '|', '^'):
            continue
        if i + 1 >= n:
            continue
        start_col = ltoks[i + 1].position[1] - 1
        end_col = _expr_end_col(ltoks, i + 1)
        if start_col < end_col:
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
            variants = [old, old + "="]
            if {old, new} == {"+", "-"}:
                variants.append(old + old)
            matches = [t for t in ltoks if t.value in variants]
            if occ < len(matches):
                t = matches[occ]
                if t.value == old:
                    return replace_at(line, t.position[1] - 1, len(old), new)
                if t.value == old + "=":
                    return replace_at(line, t.position[1] - 1, len(old) + 1, new + "=")
                if t.value == old + old:
                    return replace_at(line, t.position[1] - 1, 2, new + new)

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
        if old in (">>", ">>>"):
            t = nth_gt_run(ltoks, len(old), occ)
        else:
            t = nth_token(ltoks, old, occ)
        if t:
            return replace_at(line, t.position[1] - 1, len(old), new)
        old_c, new_c = old + "=", new + "="
        t = nth_token(ltoks, old_c, occ)
        if t:
            return replace_at(line, t.position[1] - 1, len(old_c), new_c)
        if d == "Replaced XOR with AND":
            t = nth_token(ltoks, "~", occ)
            if t:
                return replace_at(line, t.position[1] - 1, 1, "")

    if d == "changed conditional boundary":
        bmap = {">=": ">", "<=": "<", ">": ">=", "<": "<="}
        t = nth_token_in(ltoks, bmap.keys(), occ, _generic_bracket_indices(ltoks))
        if t:
            return replace_at(line, t.position[1] - 1, len(t.value), bmap[t.value])

    if d == "negated conditional":
        nmap = {"==": "!=", "!=": "==", ">=": "<", "<=": ">", ">": "<=", "<": ">="}
        t = nth_token_in(ltoks, nmap.keys(), occ, _generic_bracket_indices(ltoks))
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
            for s, e in _find_bool_assignment_rhs(ltoks):
                if s not in covered:
                    candidates.append((s, e))
                    covered.add(s)
            for s, e in _find_any_method_calls(ltoks):
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

        m2 = re.search(r'(?:if|while)\s*\(([\s\S]+)\)', line)
        if m2:
            return line[:m2.start(1)] + val + line[m2.end(1):]
        m2 = re.search(r'\breturn\s+([\s\S]+);', line)
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
            t = nth_token(ltoks, "+=", occ)
            if t:
                return replace_at(line, t.position[1] - 1, 2, "-=")
        elif old_v == -1 and new_v == 1:
            t = nth_token(ltoks, "--", occ)
            if t:
                return replace_at(line, t.position[1] - 1, 2, "++")
            t = nth_token(ltoks, "-=", occ)
            if t:
                return replace_at(line, t.position[1] - 1, 2, "+=")
        if old_v + new_v == 0 and old_v != 0:
            if old_v < 0 < new_v:
                t = nth_token(ltoks, "-=", occ)
                if t:
                    return replace_at(line, t.position[1] - 1, 2, "+=")
            elif new_v < 0 < old_v:
                t = nth_token(ltoks, "+=", occ)
                if t:
                    return replace_at(line, t.position[1] - 1, 2, "-=")
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
        result = re.sub(r'return\s+[\s\S]+?;', 'return ' + val + ';', line, count=1)
        if result != line:
            return result
        for i, t in enumerate(ltoks):
            if t.value == '->' and i + 1 < len(ltoks) and ltoks[i + 1].value != '{':
                start_col = ltoks[i + 1].position[1] - 1
                end_col = _expr_end_col(ltoks, i + 1)
                return line[:start_col] + val + line[end_col:]
        stripped = line.strip()
        if stripped == 'return' or stripped.startswith('return ') or stripped.startswith('return\t'):
            indent = line[: len(line) - len(line.lstrip())]
            return indent + 'return ' + val + ';'
        if re.match(r'\s*(?:public|private|protected|static|final|\w[\w<>,\[\]\s]*\s+\w+\s*\()', line):
            return line + " // return value replaced with " + val

    if "Changed switch default" in d:
        return line + " // switch default changed to first case"

    return line + " // MUTATED: " + d


def find_statement_span(lines, target):
    """1-based inclusive [start, end] line range of the smallest Java statement
    containing line `target`. For single-line statements returns (target, target)."""
    n = len(lines)
    if not (0 < target <= n):
        return target, target
    s = target
    while s > 1:
        prev = re.sub(r'//[^\n]*$', '', lines[s - 2]).rstrip()
        if not prev:
            break
        if prev[-1] in (';', '{', '}'):
            break
        s -= 1
    paren = brack = 0
    in_str = in_char = in_block = False
    for i in range(s - 1, n):
        line = lines[i]
        j = 0
        L = len(line)
        while j < L:
            c = line[j]
            nxt = line[j + 1] if j + 1 < L else ''
            if in_block:
                if c == '*' and nxt == '/':
                    in_block = False
                    j += 2
                    continue
                j += 1
                continue
            if in_str:
                if c == '\\':
                    j += 2
                    continue
                if c == '"':
                    in_str = False
                j += 1
                continue
            if in_char:
                if c == '\\':
                    j += 2
                    continue
                if c == "'":
                    in_char = False
                j += 1
                continue
            if c == '/' and nxt == '/':
                break
            if c == '/' and nxt == '*':
                in_block = True
                j += 2
                continue
            if c == '"':
                in_str = True
                j += 1
                continue
            if c == "'":
                in_char = True
                j += 1
                continue
            if c == '(':
                paren += 1
            elif c == ')':
                paren -= 1
            elif c == '[':
                brack += 1
            elif c == ']':
                brack -= 1
            elif c == ';' and paren == 0 and brack == 0:
                return s, i + 1
            elif c == '{' and paren == 0 and brack == 0:
                return s, i + 1
            j += 1
    return s, n


class _AdjTok:
    __slots__ = ('value', 'position')

    def __init__(self, value, position):
        self.value = value
        self.position = position


def apply_mutation_multiline(lines, stmt_tokens, desc, occ, stmt_start, stmt_end):
    """Apply `desc` to the multi-line statement on lines [stmt_start..stmt_end] by
    feeding `apply_mutation` the joined statement text with token positions adjusted
    to index into that joined string. Returns (stmt_start, stmt_end, new_text) or None.
    `new_text` may itself contain newlines and replaces the content of those lines."""
    line_starts = []
    offset = 0
    for i in range(stmt_start - 1, stmt_end):
        line_starts.append(offset)
        offset += len(lines[i]) + 1
    joined = "\n".join(lines[stmt_start - 1:stmt_end])

    adjusted = []
    for t in stmt_tokens:
        if not t.position:
            continue
        row, col = t.position
        rel = row - stmt_start
        if 0 <= rel < len(line_starts):
            adjusted.append(_AdjTok(t.value, (1, line_starts[rel] + col)))

    result = apply_mutation(joined, adjusted, desc, occ)

    fallback_marker = "// MUTATED: " + desc.strip()
    if result.endswith(fallback_marker):
        return None
    if result.strip() == joined.strip():
        return None
    return (stmt_start, stmt_end, result)


def apply_mutation_with_fallback(lines, all_tokens, lineno, desc, occ=0):
    """Returns (start_line, end_line, mutated_text). For single-line mutations
    start == end and mutated_text is one line. For multi-line, mutated_text replaces
    the content of lines [start..end] and may itself contain newlines."""
    if not (0 < lineno <= len(lines)):
        return lineno, lineno, ""
    fallback_marker = "// MUTATED: " + desc.strip()

    s, e = find_statement_span(lines, lineno)
    if e > s:
        stmt_tokens = [t for t in all_tokens
                       if t.position and s <= t.position[0] <= e]
        ml = apply_mutation_multiline(lines, stmt_tokens, desc, occ, s, e)
        if ml is not None:
            return ml

    line = lines[lineno - 1]
    ltoks = tokens_on_line(all_tokens, lineno)
    result = apply_mutation(line, ltoks, desc, occ)
    if not result.endswith(fallback_marker) and result.strip() != line.strip():
        return lineno, lineno, result

    for offset in (1, 2, -1, 3, -2, 4, 5):
        target = lineno + offset
        if not (0 < target <= len(lines)):
            continue
        tline = lines[target - 1]
        ttoks = tokens_on_line(all_tokens, target)
        tresult = apply_mutation(tline, ttoks, desc, 0)
        if tresult.endswith(fallback_marker):
            continue
        if tresult.strip() == tline.strip():
            continue
        return target, target, tresult
    return lineno, lineno, result


def _mutation_sort_key(m):
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
