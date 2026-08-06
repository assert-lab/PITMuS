#dataset generation (end-to-end: extracts mutations from XML + bytecode and writes dataset CSVs)

import csv
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

import javalang
from javalang.tokenizer import LexerError

# from pitmus_config import DATASET_VERSION, dataset_dirname


# Name of the output dataset directory (created under each system path).
# Driven by the single version constant in scripts/pitmus_config.py so the
# script and the notebook always agree; change DATASET_VERSION there.
DATASET_VERSION = "v3"
DATASET = f"PITMuS_dataset_fresh_generation-{DATASET_VERSION}"


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


def _line_for_offset(lnt_sorted, offset):
    result = None
    for off, ln in lnt_sorted:
        if off <= offset:
            result = ln
        else:
            break
    return result


# ---------------------------------------------------------------------------
# Faithful reconstruction of the EXPERIMENTAL_SWITCH mutator
# ("Changed switch default to be first case").
#
# PIT's SwitchMutator (verified against pitest 1.22.0) is NOT a pairwise
# default<->first-case swap. `swapLabels` does a wholesale redirection of the
# jump table:
#     newDefault = first case label (smallest key) whose target != default
#     switch default        -> newDefault           (old FIRST-CASE body)
#     every real case label -> old default target   (old DEFAULT body)
#     (a case that shared the default's target is repointed to newDefault too;
#      tableswitch "gap" keys behave like default and so also land on newDefault)
#
# Net observable effect at source level: every real case runs the OLD DEFAULT
# body, and `default:` (plus any default-sharing/gap keys) runs the OLD
# FIRST-CASE body. This is a whole-switch-block rewrite, not a one-line edit,
# and identifying the "first case" needs the bytecode jump-table keys (many
# bcel switches key on symbolic Const.* opcodes not resolvable from source).
#
# We therefore read the switch table straight from `javap -c -p -l` and rewrite
# the source block. Everything is hard-gated: if the block cannot be parsed
# cleanly (fall-through into the first case, nested switch, default body that
# maps outside the block, no source `default`, or the rewrite fails to re-parse)
# we return None and the caller falls back to the comment no-op (scored
# UNVERIFIABLE by the faithfulness oracle) rather than emit a wrong mutant.
# ---------------------------------------------------------------------------

_SWITCH_TERM_RE = re.compile(r'^\s*(break|return|throw|continue|yield)\b')
_SWITCH_LABEL_RE = re.compile(r'^\s*(case\b.+?|default)\s*:(.*)$')


def parse_switch_tables(text):
    """Parse `javap -c -p -l` output into per-Code-region records:
        [{"switches": [{offset, kind, default, cases:[(key,target)]}],
          "lnt": [(bytecode_offset, source_line), ...]}]
    Keyed on `Code:` markers so it works without `javap -v` (which the existing
    descriptor-based parser needs and which `-c -p -l` does not emit)."""
    regions = []
    lines = text.splitlines()
    cur = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"\s+Code:\s*$", line):
            cur = {"switches": [], "lnt": []}
            regions.append(cur)
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        m = re.match(r"\s+(\d+):\s+(tableswitch|lookupswitch)\s*\{", line)
        if m:
            off = int(m.group(1)); kind = m.group(2)
            cases = []; default = None
            j = i + 1
            while j < len(lines):
                s = lines[j].strip()
                if s == "}":
                    break
                dm = re.match(r"default:\s+(\d+)", s)
                km = re.match(r"(-?\d+):\s+(\d+)", s)
                if dm:
                    default = int(dm.group(1))
                elif km:
                    cases.append((int(km.group(1)), int(km.group(2))))
                j += 1
            cur["switches"].append({"offset": off, "kind": kind,
                                    "default": default, "cases": cases})
            i = j + 1
            continue
        lm = re.match(r"\s+line\s+(\d+):\s+(\d+)\s*$", line)
        if lm:
            cur["lnt"].append((int(lm.group(2)), int(lm.group(1))))
        i += 1
    return regions


def load_switch_tables(class_file):
    if not os.path.exists(class_file):
        return []
    try:
        out = subprocess.run(
            ["javap", "-c", "-p", "-l", class_file],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    return parse_switch_tables(out.stdout)


def _find_bytecode_switch(regions, pit_line):
    """Return (dist, header_src_line, switch_dict, sorted_lnt) for the switch
    whose header source line is nearest pit_line, or None."""
    best = None
    for cur in regions:
        lnt = sorted(cur["lnt"])
        for sw in cur["switches"]:
            swline = _line_for_offset(lnt, sw["offset"])
            if swline is None:
                continue
            d = abs(swline - pit_line)
            if best is None or d < best[0]:
                best = (d, swline, sw, lnt)
    return best


def _switch_scan_braces(lines, start):
    """String/char/comment-aware brace scan from line `start` (0-based). Return
    0-based line index of the matching close brace, or None."""
    depth = 0; started = False; in_block = False
    for j in range(start, len(lines)):
        line = lines[j]
        k = 0
        while k < len(line):
            c = line[k]; nxt = line[k + 1] if k + 1 < len(line) else ""
            if in_block:
                if c == '*' and nxt == '/':
                    in_block = False; k += 2; continue
                k += 1; continue
            if c == '/' and nxt == '/':
                break
            if c == '/' and nxt == '*':
                in_block = True; k += 2; continue
            if c == '"':
                k += 1
                while k < len(line):
                    if line[k] == '\\':
                        k += 2; continue
                    if line[k] == '"':
                        break
                    k += 1
                k += 1; continue
            if c == "'":
                k += 1
                while k < len(line):
                    if line[k] == '\\':
                        k += 2; continue
                    if line[k] == "'":
                        break
                    k += 1
                k += 1; continue
            if c == '{':
                depth += 1; started = True
            elif c == '}':
                depth -= 1
                if started and depth == 0:
                    return j
            k += 1
    return None


def _find_switch_block(lines, pit_line):
    sw = None
    for j in range(max(0, pit_line - 3), min(len(lines), pit_line + 2)):
        if re.search(r'\bswitch\s*\(', lines[j]):
            sw = j; break
    if sw is None:
        return None
    end = _switch_scan_braces(lines, sw)
    if end is None:
        return None
    return (sw, end)


def _parse_switch_segments(lines, s_idx, e_idx):
    """Split the switch body into ordered segments:
        {labels:[str], body:[str], is_default:bool, first_body_line:int|None}
    Inline `case X: stmt` is split. Returns (segments, reason)."""
    for k in range(s_idx + 1, e_idx):
        if re.search(r'\bswitch\s*\(', lines[k]):
            return None, "nested switch"
    segments = []
    cur = None
    k = s_idx + 1
    while k < e_idx:
        raw = lines[k]
        if not raw.strip():
            if cur is not None:
                cur["body"].append(raw)
            k += 1
            continue
        if cur is None and raw.strip() == "{":
            k += 1
            continue
        m = _SWITCH_LABEL_RE.match(raw)
        if m:
            is_def = m.group(1).strip() == "default"
            trailing = m.group(2).strip()
            if cur is None or cur["body_started"]:
                cur = {"labels": [], "body": [], "is_default": False,
                       "body_started": False, "first_body_line": None}
                segments.append(cur)
            indent = re.match(r'\s*', raw).group(0)
            cur["labels"].append(indent + m.group(1).strip() + ":")
            if is_def:
                cur["is_default"] = True
            if trailing:
                cur["body"].append(indent + "    " + trailing)
                cur["body_started"] = True
                cur["first_body_line"] = k + 1
        else:
            if cur is None:
                return None, "stmt before label"
            cur["body"].append(raw)
            cur["body_started"] = True
            if cur["first_body_line"] is None:
                cur["first_body_line"] = k + 1
        k += 1
    return segments, "ok"


def _switch_body_terminated(body):
    """True if the body's last top-level statement is break/return/throw/
    continue/yield (no fall-through). Keys on the base (minimum) indent so
    multi-line statements are handled correctly."""
    code = [ln for ln in body if ln.strip()]
    if not code:
        return False
    base = min(len(ln) - len(ln.lstrip()) for ln in code)
    last_base = None
    for ln in code:
        if len(ln) - len(ln.lstrip()) == base:
            last_base = ln
    return bool(last_base and _SWITCH_TERM_RE.match(last_base))


def reconstruct_switch_default(lines, pit_line, regions):
    """Faithfully reconstruct the EXPERIMENTAL_SWITCH mutation on the switch at
    `pit_line`. Return (start_line, end_line, new_block_text) (1-based, inclusive)
    or None if the block cannot be reconstructed safely."""
    blk = _find_switch_block(lines, pit_line)
    if not blk:
        return None
    s_idx, e_idx = blk
    best = _find_bytecode_switch(regions, pit_line)
    if not best:
        return None
    d, swline, sw, lnt = best
    if d > 2:
        return None
    default_t = sw["default"]
    if default_t is None:
        return None
    newDefault = None
    for kk, tt in sorted(sw["cases"], key=lambda kt: kt[0]):
        if tt != default_t:
            newDefault = tt
            break
    if newDefault is None:
        return None
    default_body_line = _line_for_offset(lnt, default_t)
    firstcase_body_line = _line_for_offset(lnt, newDefault)
    if not default_body_line or not firstcase_body_line:
        return None

    segs, why = _parse_switch_segments(lines, s_idx, e_idx)
    if segs is None:
        return None
    defsegs = [g for g in segs if g["is_default"]]
    if len(defsegs) != 1:
        return None
    defseg = defsegs[0]
    caseg = [g for g in segs if not g["is_default"]]
    if not caseg:
        return None

    def seg_containing(ln0):
        for gi, g in enumerate(segs):
            start = g["first_body_line"]
            if start is None:
                continue
            nxt = None
            for h in segs[gi + 1:]:
                if h["first_body_line"] is not None:
                    nxt = h["first_body_line"]
                    break
            end = (nxt - 1) if nxt else e_idx
            if start <= ln0 <= end:
                return g
        return None

    dseg = seg_containing(default_body_line)
    fseg = seg_containing(firstcase_body_line)
    if dseg is None or fseg is None:
        return None
    if dseg is not defseg or fseg["is_default"]:
        return None
    # first-case body must be terminator-ended, else its effective (fall-through)
    # body extends past the segment we captured -> unsafe.
    if not _switch_body_terminated(fseg["body"]):
        return None

    label_indent = re.match(r'\s*', caseg[0]["labels"][0]).group(0)
    body_indent = label_indent + "    "
    default_body = list(defseg["body"])
    if not _switch_body_terminated(default_body):
        # original default fell out of the switch; promoted before `default:` it
        # needs an explicit break to preserve that (and avoid fall-through).
        default_body = default_body + [body_indent + "break;"]

    brace_line = s_idx
    for j in range(s_idx, e_idx):
        if '{' in lines[j]:
            brace_line = j
            break
    out = list(lines[s_idx:brace_line + 1])
    for g in caseg:
        out.extend(g["labels"])
    out.extend(default_body)                 # every real case -> OLD DEFAULT body
    out.append(label_indent + "default:")
    out.extend(fseg["body"])                 # default -> OLD FIRST-CASE body
    out.append(lines[e_idx])
    text = "\n".join(out)

    # Validate: substitute the block back and re-parse the whole file.
    newfile = lines[:s_idx] + text.split("\n") + lines[e_idx + 1:]
    try:
        javalang.parse.parse("\n".join(newfile))
    except Exception:
        return None
    return (s_idx + 1, e_idx + 1, text)


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


def extract_javadoc(lines, method_start):
    i = method_start - 2
    while i >= 0 and (lines[i].strip() == "" or lines[i].lstrip().startswith("@")):
        i -= 1
    if i < 0 or not lines[i].rstrip().endswith("*/"):
        return ""
    end = i
    while i >= 0 and "/**" not in lines[i]:
        i -= 1
    if i < 0:
        return ""
    return "\n".join(lines[i:end + 1])


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


def nth_token_in(toks, values, n):
    count = 0
    for t in toks:
        if t.value in values:
            if count == n:
                return t
            count += 1
    return None


def _generic_bracket_indices(ltoks):
    """Indices of `<` / `>` tokens that act as generic-type delimiters
    (`ArrayList<>`, `Map<String, ?>`, `List<List<T>>`) rather than relational
    operators. PIT counts conditional-boundary / relational mutants by their
    bytecode operator instances, and generic brackets have no bytecode presence
    -- so counting them as `<`/`>` operators shifts the occurrence index and
    picks the wrong token (e.g. mutating `ArrayList<>` into `ArrayList<=>`).

    A `<` opens a generic list only when it follows a type-name identifier and a
    balanced matching `>` is found containing only type-list tokens; otherwise a
    genuine comparison such as `a < b` is left untouched."""
    result = set()
    n = len(ltoks)
    _type_ok = {'.', ',', '?', 'extends', 'super', '[', ']', '<', '>', '>>', '>>>'}
    i = 0
    while i < n:
        if ltoks[i].value == '<' and i > 0:
            prev = ltoks[i - 1].value
            if prev.isidentifier() and prev[:1].isupper():
                depth = 0
                members = []
                ok = True
                j = i
                while j < n:
                    v = ltoks[j].value
                    if v == '<':
                        depth += 1
                        members.append(j)
                    elif v in ('>', '>>', '>>>'):
                        depth -= len(v)
                        members.append(j)
                        if depth <= 0:
                            break
                    elif v in _type_ok or (v.isidentifier() and v not in (
                            'return', 'new', 'instanceof', 'true', 'false', 'null')):
                        pass
                    else:
                        ok = False
                        break
                    j += 1
                if ok and depth <= 0:
                    result.update(members)
                    i = j + 1
                    continue
        i += 1
    return result


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
        elif depth == 0 and v in (
            '||', '&&', ';', '{', ',', '?', ':', 'return', 'throw',
            '=', '==', '!=',
            # compound-assignment operators are statement-level boundaries too:
            # without them `_expr_start_col` walks back past `len +=` and folds
            # the accumulator into the branch (`len += cond ? a : b` -> `cond`).
            '+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=', '<<=', '>>=', '>>>=',
        ):
            break
        first_i = i
    t = ltoks[first_i]
    return t.position[1] - 1


def _cond_expr_start(ltoks, end_idx):
    """Like _expr_start_col but does NOT stop at `==`/`!=`, so it spans a full
    equality condition. Used only to compute a ternary's condition range for the
    dedup check: `msg != null ? a : b` has its `!=` already captured as a
    comparison candidate, and stopping at `!=` would make the ternary pass add a
    truncated phantom candidate covering just the right operand (`null`), which
    both mis-folds (`msg != false`) and shifts PIT's occurrence numbering."""
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
        elif depth == 0 and v in (
            '||', '&&', ';', '{', ',', '?', ':', 'return', 'throw',
            '=', '+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=',
            '<<=', '>>=', '>>>=',
        ):
            break
        first_i = i
    t = ltoks[first_i]
    return t.position[1] - 1


def _type_end_col(ltoks, start_idx):
    """End column of a type reference beginning at start_idx (the token right
    after `instanceof`). Consumes a dotted qualified name, a balanced generic
    argument list `<...>` (wildcards `<?>`, `<? extends T>`, nested generics),
    and trailing array brackets `[]`.

    `_expr_end_col` treats `?` and `<`/`>` as expression boundaries, so on
    `x instanceof Comparable<?>` it stops at `<`, leaving a dangling `?>` after
    the false-substitution (`if (false?>)`, broke syntax). Walking the type
    grammar here captures the whole `Comparable<?>` so it is replaced cleanly."""
    n = len(ltoks)
    i = start_idx
    last_i = start_idx
    while i + 2 < n and ltoks[i + 1].value == '.':
        i += 2
        last_i = i
    if i + 1 < n and ltoks[i + 1].value == '<':
        depth = 0
        j = i + 1
        while j < n:
            v = ltoks[j].value
            if v == '<':
                depth += 1
            elif v in ('>', '>>', '>>>'):
                depth -= len(v)
                if depth <= 0:
                    last_i = j
                    i = j
                    break
            j += 1
    while i + 2 < n and ltoks[i + 1].value == '[' and ltoks[i + 2].value == ']':
        i += 2
        last_i = i
    t = ltoks[last_i]
    return t.position[1] - 1 + len(t.value)


def _find_instanceof_spans(ltoks):
    """Span of each `LHS instanceof RHS` boolean sub-expression, computed by
    balanced token walking (via _expr_start_col/_type_end_col) so a complex left
    operand -- casts, receiver chains, nested calls such as
    `((ArrayType) x).getElementType()` -- is captured in full, and a generic
    right operand `Comparable<?>` is captured through its type arguments.

    The previous regex `(\\w[\\w.]*(?:\\(\\))?)\\s+instanceof\\s+(\\w[\\w.]*)`
    only matched a bare-identifier LHS, so on anything else it grabbed the wrong
    start column and produced garbage like `arrayreffalse` (broke syntax)."""
    spans = []
    for i, tok in enumerate(ltoks):
        if tok.value != 'instanceof' or i == 0 or i + 1 >= len(ltoks):
            continue
        start_col = _expr_start_col(ltoks, i - 1)
        end_col = _type_end_col(ltoks, i + 1)
        if start_col < end_col:
            spans.append((start_col, end_col))
    return spans


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
        start_col = _expr_start_col(ltoks, i - 1)
        end_col = _expr_end_col(ltoks, i)
        if end_col > start_col:
            calls.append((start_col, end_col))
    return calls


def _branch_cond_span(ltoks):
    """For a leading `if (...)` / `while (...)` (incl. `else if (...)`), return the
    (start_col, end_col) of the text *inside* the matching parentheses, else None.

    A braceless single-statement branch spanning two lines
    (`if (cond)\n    body;`) is captured by find_statement_span as one joined
    statement. Backward expression scans (`_expr_start_col`) then run past the
    condition's closing `)` into the body because no `;`/`{` separates them,
    yielding a bogus fold span that swallows the whole statement (`false;`).
    Callers use this span to keep a "removed conditional" fold inside the branch
    predicate, preserving `if (`, `)`, and the trailing body statement."""
    for i, t in enumerate(ltoks):
        if t.value in ('if', 'while') and i + 1 < len(ltoks) and ltoks[i + 1].value == '(':
            op = ltoks[i + 1]
            depth = 0
            for j in range(i + 1, len(ltoks)):
                v = ltoks[j].value
                if v == '(':
                    depth += 1
                elif v == ')':
                    depth -= 1
                    if depth == 0:
                        return (op.position[1], ltoks[j].position[1] - 1)
            return None
    return None


def _for_cond_range(ltoks):
    """For a `for (init; cond; update)` header, return the (start_col, end_col)
    of the condition section (between the 1st and 2nd top-level ';'), else None.

    Only the condition of a for-loop holds a foldable branch; calls in the init
    (`for (Iterator it = values(); ...)`) or update section are not conditionals.
    The last-resort bare-call finder must therefore be restricted to this range,
    otherwise an init-RHS call is (wrongly) folded to true/false. Returns None
    for non-for lines and enhanced-for (`for (x : xs)`), leaving them unchanged."""
    fi = next((i for i, t in enumerate(ltoks) if t.value == 'for'), None)
    if fi is None or fi + 1 >= len(ltoks) or ltoks[fi + 1].value != '(':
        return None
    depth = 0
    semis = []
    for i in range(fi + 1, len(ltoks)):
        v = ltoks[i].value
        if v in ('(', '['):
            depth += 1
        elif v in (')', ']'):
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and v == ';':
            semis.append(i)
    if len(semis) < 2 or semis[0] + 1 > semis[1] - 1:
        return None
    a, b = semis[0] + 1, semis[1] - 1
    start_col = ltoks[a].position[1] - 1
    end_col = ltoks[b].position[1] - 1 + len(ltoks[b].value)
    return (start_col, end_col)


_RELOPS = ('==', '!=', '<', '>', '<=', '>=')
_OPERAND_STOP = ('||', '&&', ';', '{', '}', ',', '?', ':', 'return', 'throw', '=')


def _operand_left_start(ltoks, end_idx):
    """Index of the first token of the operand that ENDS at end_idx, walking back
    with bracket balancing and stopping at a boolean connective / statement
    boundary (but NOT at a relational operator, so the whole `a == b` operand is
    returned rather than just `b`)."""
    depth = 0
    first = end_idx
    for i in range(end_idx, -1, -1):
        v = ltoks[i].value
        if v in (')', ']'):
            depth += 1
        elif v in ('(', '['):
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and v in _OPERAND_STOP:
            break
        first = i
    return first


def _operand_right_end(ltoks, start_idx):
    """Index of the last token of the operand that STARTS at start_idx."""
    depth = 0
    last = start_idx
    for i in range(start_idx, len(ltoks)):
        v = ltoks[i].value
        if v in ('(', '['):
            depth += 1
        elif v in (')', ']'):
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and v in _OPERAND_STOP:
            break
        last = i
    return last


def _range_has_toplevel(ltoks, a, b, ops):
    depth = 0
    for i in range(a, b + 1):
        v = ltoks[i].value
        if v in ('(', '['):
            depth += 1
        elif v in (')', ']'):
            depth -= 1
        elif depth == 0 and v in ops:
            return True
    return False


def _is_leaf_bool(ltoks, a, b):
    """True if tokens [a..b] form a single boolean leaf: after stripping a
    balanced outer paren wrapper and any leading `!`, the region contains no
    top-level relational operator and no `&&`/`||`. A leaf compiles to one
    branch instruction (IFEQ/IFNE) that PIT's removed-conditional can flip; a
    region with a relop is a comparison already enumerated by the comp-op pass,
    and a region with `&&`/`||` is compound (its own operands are enumerated
    separately)."""
    # strip balanced outer parens
    while a < b and ltoks[a].value == '(':
        depth = 0
        match = -1
        for k in range(a, b + 1):
            if ltoks[k].value == '(':
                depth += 1
            elif ltoks[k].value == ')':
                depth -= 1
                if depth == 0:
                    match = k
                    break
        if match == b:
            a += 1
            b -= 1
        else:
            break
    while a <= b and ltoks[a].value == '!':
        a += 1
    if a > b:
        return False
    if _range_has_toplevel(ltoks, a, b, _RELOPS):
        return False
    if _range_has_toplevel(ltoks, a, b, ('&&', '||')):
        return False
    return True


def _find_compound_bool_subexprs(ltoks):
    """Leaf bare-boolean operands of `&&`/`||` (e.g. `!found`, `x.isValid()`).
    Relational operands (`a == b`) are skipped -- the comp-op pass already emits
    the full comparison span; emitting a partial operand here would corrupt the
    substitution (`a == false`) and shift PIT's occurrence numbering."""
    results = []
    n = len(ltoks)
    for i, tok in enumerate(ltoks):
        if tok.value not in ('&&', '||'):
            continue
        # right operand
        if i + 1 < n and ltoks[i + 1].value not in ('&&', '||', ';', '{', '}', ',', '?', ':', ')'):
            r_start = i + 1
            r_end = _operand_right_end(ltoks, r_start)
            if r_start <= r_end and _is_leaf_bool(ltoks, r_start, r_end):
                s = ltoks[r_start].position[1] - 1
                e = ltoks[r_end].position[1] - 1 + len(ltoks[r_end].value)
                if s < e:
                    results.append((s, e))
        # left operand
        if i > 0 and ltoks[i - 1].value not in ('&&', '||', '(', '{', ',', ';', '?', ':', '!'):
            l_end = i - 1
            l_start = _operand_left_start(ltoks, l_end)
            if l_start <= l_end and _is_leaf_bool(ltoks, l_start, l_end):
                s = ltoks[l_start].position[1] - 1
                e = ltoks[l_end].position[1] - 1 + len(ltoks[l_end].value)
                if s < e:
                    results.append((s, e))
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
        rhs_end = _operand_right_end(ltoks, i + 1)
        # Only a boolean-valued RHS is a removable-conditional branch. A plain
        # numeric/reference init (`int i = 0`, `n = Const.T_BOOLEAN`) is not: its
        # RHS holds no branch, so replacing it with true/false both breaks
        # compilation and steals the occurrence index from the real comparison.
        toks_rhs = [t.value for t in ltoks[i + 1:rhs_end + 1]]
        # A bare method-call/constructor/parenthesized RHS is NOT reliably a
        # boolean branch: `InstructionHandle ih = il.getStart()`,
        # `StringTokenizer t = new StringTokenizer(...)`, and
        # `String s = "x" + (a ? b : c)` all contain '(' but yield references,
        # not a foldable IFEQ.  Requiring a top-level relational/logical/negation
        # operator (or a literal true/false) keeps only genuine boolean RHSs.
        # A truly bare boolean call (`boolean b = list.isEmpty()`) is still
        # recovered by the `_find_any_method_calls` last-resort below.
        is_bool = (
            _range_has_toplevel(ltoks, i + 1, rhs_end, _RELOPS)
            or _range_has_toplevel(ltoks, i + 1, rhs_end, ('&&', '||'))
            or 'instanceof' in toks_rhs
            or '!' in toks_rhs
            or (len(toks_rhs) == 1 and toks_rhs[0] in ('true', 'false'))
        )
        if not is_bool:
            continue
        start_col = ltoks[i + 1].position[1] - 1
        end_col = _expr_end_col(ltoks, i + 1)
        if start_col < end_col:
            results.append((start_col, end_col))
    return results


def _hoist_sideeffect_cond(line, val):
    """Reconstruct a removed-conditional mutant when the branch condition
    contains a top-level assignment side effect, e.g.

        if ((flags = readFlags()) != 0) { ...

    PIT keeps the assignment's store instruction and only forces the branch
    constant.  A naive `if (false)` reconstruction deletes the assignment and
    won't compile (the assigned variable is later "might not have been
    initialized").  We instead hoist the assignment out and constant-fold the
    branch, preserving the side effect:

        flags = readFlags();
        if (false) { ...

    Returns the reconstructed multi-line string, or None if the condition is
    not a single top-level parenthesized assignment comparison."""
    m = re.match(r'(\s*)if\s*\(', line)
    if not m:
        return None
    indent = m.group(1)
    cond_open = m.end() - 1  # index of the '(' opening the if-condition

    # Balanced scan to find the matching ')' of the if-condition.
    depth = 0
    cond_close = -1
    for j in range(cond_open, len(line)):
        c = line[j]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                cond_close = j
                break
    if cond_close < 0:
        return None

    cond = line[cond_open + 1:cond_close]
    tail = line[cond_close + 1:]  # `) { ...` remainder, keep verbatim

    # Reject compound conditions: only a single comparison can be folded here.
    if '&&' in cond or '||' in cond or '?' in cond:
        return None

    # The condition must open with a parenthesized assignment: `( VAR = ... )`.
    cm = re.match(r'\s*\(\s*([A-Za-z_$][\w$.\[\]]*)\s*=(?!=)', cond)
    if not cm:
        return None
    var = cm.group(1)

    # Extract the assignment's RHS by balanced-scanning to the assignment's
    # closing ')'.
    assign_open = cond.index('(')
    depth = 0
    assign_close = -1
    for j in range(assign_open, len(cond)):
        c = cond[j]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                assign_close = j
                break
    if assign_close < 0:
        return None
    eq = cond.index('=', assign_open)
    rhs = cond[eq + 1:assign_close].strip()
    if not rhs:
        return None

    return f'{indent}{var} = {rhs};\n{indent}if ({val}){tail}'


def _find_call_span(line, name):
    """Locate `name(...)` (with any leading dotted receiver chain) on `line`,
    matching parentheses so nested calls/expressions in the argument list are
    handled. Returns (start, end) covering `<receiver>.name(...)`, or None if the
    call is not on this line or its parentheses don't close on this line.

    The old `\\([^()]*\\)` regex could not match a call whose arguments contained
    their own parentheses (e.g. `foo(bar(x))`), which silently produced a
    comment-only, no-op mutant for the whole VoidMethodCall family."""
    spans = []
    for m in re.finditer(r'\b' + re.escape(name) + r'\s*\(', line):
        i = m.start()
        if i > 0 and (line[i - 1].isalnum() or line[i - 1] in "_$"):
            continue  # `name` is part of a longer identifier
        depth = 0
        j = m.end() - 1  # index of the opening '('
        in_str = in_chr = False
        closed = False
        while j < len(line):
            c = line[j]
            # Skip string/char literals so parentheses (and quotes) inside them
            # don't corrupt the balance, e.g. println(")</B>") or println("a;b").
            if in_str:
                if c == "\\":
                    j += 2
                    continue
                if c == '"':
                    in_str = False
            elif in_chr:
                if c == "\\":
                    j += 2
                    continue
                if c == "'":
                    in_chr = False
            elif c == '"':
                in_str = True
            elif c == "'":
                in_chr = True
            elif c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    closed = True
                    break
            j += 1
        if not closed:
            continue  # parentheses don't close on this line (multi-line call)
        end = j + 1
        rec = re.search(r'(?:[\w$]+\s*\.\s*)+$', line[:i])
        start = rec.start() if rec else i
        spans.append((start, end))
    if not spans:
        return None
    # A braceless branch (`else if (tq.matches(...))\n body(...)`) joins the
    # header and body into one text. A same-named call in the *condition*
    # (`tq.matches`) precedes the real removed body call; folding it empties the
    # predicate (`else if ()`). Prefer a call outside the leading branch
    # condition's parentheses when one exists.
    cond = _leading_cond_paren_span(line)
    if cond is not None:
        outside = [sp for sp in spans if not (cond[0] <= sp[0] < cond[1])]
        if outside:
            return outside[0]
    return spans[0]


def _leading_cond_paren_span(line):
    """(start, end) of the parentheses of a leading `if (...)` / `while (...)`
    (incl. `} else if (...)`) at the head of `line`, string-literal aware; else
    None. Used to keep a removed-call fold off the branch predicate."""
    m = re.match(r'\s*(\}\s*)?(else\s+)?(if|while)\s*\(', line)
    if not m:
        return None
    j = m.end() - 1  # index of the opening '('
    depth = 0
    in_str = in_chr = False
    while j < len(line):
        c = line[j]
        if in_str:
            if c == "\\":
                j += 2
                continue
            if c == '"':
                in_str = False
        elif in_chr:
            if c == "\\":
                j += 2
                continue
            if c == "'":
                in_chr = False
        elif c == '"':
            in_str = True
        elif c == "'":
            in_chr = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return (m.end() - 1, j + 1)
        j += 1
    return None


def _toplevel_semicolon(line, start):
    """Index of the first ';' at bracket-depth 0 and outside any string/char
    literal, scanning from `start`; or -1 if none on this line. Used to find the
    real end of a statement so a ';' inside a literal (e.g. `">;"`) or nested
    parentheses doesn't truncate it."""
    depth = 0
    in_str = in_chr = False
    k = start
    while k < len(line):
        c = line[k]
        if in_str:
            if c == "\\":
                k += 2
                continue
            if c == '"':
                in_str = False
        elif in_chr:
            if c == "\\":
                k += 2
                continue
            if c == "'":
                in_chr = False
        elif c == '"':
            in_str = True
        elif c == "'":
            in_chr = True
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth:
                depth -= 1
        elif c == ';' and depth == 0:
            return k
        k += 1
    return -1


def _is_concat_plus(ltoks, idx):
    """True if the `+`/`+=` at `ltoks[idx]` is String concatenation rather than
    numeric addition. PIT's math mutator only touches bytecode IADD, so
    concatenation `+` must be excluded from occurrence numbering -- otherwise
    `"L" + s.substring(consumed_chars+1)` folds the string `+` and steals the
    index from the real integer `+`.

    A `+`/`-` additive chain is String concatenation iff any operand in the
    chain (at the same paren depth) is a String literal. `*`, `/`, relational
    and boundary tokens end the chain; `.`/identifiers/numbers are operand
    atoms. Nested-paren operands (e.g. method-call arguments) are a separate
    depth and are not inspected."""
    _STOP = ('+', '-')  # tokens that continue an additive chain
    _BOUND = (',', ';', '?', ':', '=', '==', '!=', '<', '>', '<=', '>=',
              '&&', '||', '*', '/', '%', '&', '|', '^', '<<', '>>', '>>>',
              '+=', '-=', '*=', '/=', '%=', 'return', 'throw',
              'instanceof', '{')

    def _is_str(v):
        return len(v) >= 2 and v[0] == '"'

    # scan left
    depth = 0
    j = idx - 1
    while j >= 0:
        v = ltoks[j].value
        if v in (')', ']'):
            depth += 1
        elif v in ('(', '['):
            if depth == 0:
                break
            depth -= 1
        elif depth == 0:
            if _is_str(v):
                return True
            if v in _STOP:
                pass
            elif v in _BOUND:
                break
        j -= 1
    # scan right
    depth = 0
    j = idx + 1
    n = len(ltoks)
    while j < n:
        v = ltoks[j].value
        if v in ('(', '['):
            depth += 1
        elif v in (')', ']'):
            if depth == 0:
                break
            depth -= 1
        elif depth == 0:
            if _is_str(v):
                return True
            if v in _STOP:
                pass
            elif v in _BOUND:
                break
        j += 1
    return False


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
            matches = [
                t for i, t in enumerate(ltoks)
                if t.value in variants
                # Exclude String-concatenation `+`/`+=`: PIT never mutates it.
                and not (old == "+" and t.value in ("+", "+=")
                         and _is_concat_plus(ltoks, i))
            ]
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
        generic = _generic_bracket_indices(ltoks)
        count = 0
        t = None
        for i_tok, tok in enumerate(ltoks):
            if tok.value in bmap and i_tok not in generic:
                if count == occ:
                    t = tok
                    break
                count += 1
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

        # A branch condition with a top-level assignment side effect
        # (`if ((v = expr) OP operand)`) cannot be folded in place: dropping
        # the whole condition drops the assignment and leaves `v` possibly
        # uninitialized.  Hoist the assignment and constant-fold the branch.
        if occ == 0:
            hoisted = _hoist_sideeffect_cond(line, val)
            if hoisted is not None:
                return hoisted

        candidates = []

        generic = _generic_bracket_indices(ltoks) if not is_equality else set()
        for i_tok, tok in enumerate(ltoks):
            if tok.value in comp_ops and 0 < i_tok < len(ltoks) - 1 \
                    and i_tok not in generic:
                candidates.append((
                    _expr_start_col(ltoks, i_tok - 1),
                    _expr_end_col(ltoks, i_tok + 1),
                ))

        if is_equality:
            for s, e in _find_eq_method_calls(ltoks):
                candidates.append((s, e))

            for s, e in _find_instanceof_spans(ltoks):
                candidates.append((s, e))

            def _overlaps(s, e):
                return any(not (e <= cs or ce <= s) for cs, ce in candidates)

            for s, e in _find_compound_bool_subexprs(ltoks):
                if not _overlaps(s, e):
                    candidates.append((s, e))
            for s, e in _find_bool_assignment_rhs(ltoks):
                if not _overlaps(s, e):
                    candidates.append((s, e))

        for i_tok, tok in enumerate(ltoks):
            if tok.value == '?' and i_tok > 0:
                prev = ltoks[i_tok - 1]
                nxt = ltoks[i_tok + 1] if i_tok + 1 < len(ltoks) else None
                # A `?` inside a generic type argument is a wildcard
                # (`Class<?>`, `Map<K, ?>`, `<? extends T>`, `<? super T>`),
                # not a ternary conditional. Folding it (`false?>`) corrupts
                # the line and steals PIT's occurrence index from the real
                # comparison. A genuine ternary `?` never follows `<`/`,` nor
                # precedes `>`/`extends`/`super`.
                if prev.value in ('<', ',') or (
                        nxt is not None and nxt.value in ('>', 'extends', 'super')):
                    continue
                end_col = prev.position[1] - 1 + len(prev.value)
                start_col = _expr_start_col(ltoks, i_tok - 1)
                # Dedup against a comparison that already spans the whole
                # condition: `msg != null ?` has `!=` captured above, so the
                # ternary must not re-add a truncated `null` phantom. The
                # containment check uses a start that spans through `==`/`!=`.
                cond_start = _cond_expr_start(ltoks, i_tok - 1)
                if not any(cond_start <= s and e <= end_col for s, e in candidates):
                    candidates.append((start_col, end_col))

        # Last resort only: a bare boolean-returning call used as the sole
        # condition (`if (x.isValid())`). Running this when real candidates
        # already exist would add non-boolean calls (method arguments) and
        # corrupt PIT's occurrence numbering.
        if is_equality and not candidates:
            fcr = _for_cond_range(ltoks)
            for s, e in _find_any_method_calls(ltoks):
                # In a for-header, only calls in the condition section are
                # conditionals; init/update calls (`for (it = values(); ...)`)
                # must not be folded.
                if fcr is not None and not (fcr[0] <= s and e <= fcr[1]):
                    continue
                candidates.append((s, e))

        # Multi-line braceless branch (`if (cond)\n body;`): a backward expression
        # scan from a body call crosses the condition's closing `)` (no `;`/`{`
        # separates header and body), producing a span that straddles the
        # boundary and swallows the whole statement -> `false;`/`true;`, which is
        # not a valid statement. Drop any candidate that crosses the condition
        # close; keep folds fully inside the condition or fully in the body. If
        # none remain, the fallback below folds the whole condition, preserving
        # `if (`, `)`, and the body.
        if "\n" in line:
            cspan = _branch_cond_span(ltoks)
            if cspan is not None:
                _, cend = cspan
                candidates = [(s, e) for (s, e) in candidates if e <= cend or s >= cend]

        candidates.sort(key=lambda x: x[0])
        if occ < len(candidates):
            s, e = candidates[occ]
            return line[:s] + val + line[e:]

        cspan = _branch_cond_span(ltoks)
        if cspan is not None:
            cstart, cend = cspan
            return line[:cstart] + val + line[cend:]
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
        span = _find_call_span(line, name)
        if span:
            start, end = span
            # Expression-lambda body is the removed void call
            # (`e -> handler(e)`): deleting the call leaves `e -> )` which
            # won't parse. Replace the body with an empty block `e -> {}`
            # (a valid do-nothing statement lambda) instead.
            if line[:start].rstrip().endswith('->'):
                return (line[:start] + "{}" + line[end:]
                        + "  // removed call to " + name + "()")
            # Full-statement void call: the call (plus an optional ';') is the
            # whole statement -> the mutant removes the entire statement.
            # Strip a trailing line comment first so `foo(); // note` still
            # counts as a full statement (not an embedded call).
            tail = re.sub(r'//.*$', '', line[end:]).strip()
            if tail in ("", ";"):
                # If the void call is the body of one or more switch-case /
                # default labels (a multi-line statement PITMuS captured with
                # its leading labels), collapsing the whole thing to a comment
                # deletes the labels and the switch no longer parses. Keep the
                # labels and neutralize just the call with an empty statement.
                prefix = line[:start]
                prefix_stripped = prefix.rstrip()
                is_switch_label = (prefix_stripped.endswith(':')
                                   and re.search(r'\bcase\b|\bdefault\b', prefix))
                if is_switch_label:
                    return prefix + ";  // removed call to " + name + "()"
                # Braceless single-statement branch body captured together with
                # its header (`if (c)\n call();`, `} else\n call();`,
                # `else if (c)\n <cast>.call();`): the removed call is the sole
                # body. Commenting the whole statement out deletes the header and
                # leaves a dangling branch (or drops a closing `}`). Keep the
                # header and neutralize the body with an empty statement. Detect
                # from the head of the joined statement so a dotted/cast receiver
                # chain in the prefix does not hide the branch.
                head = line.lstrip()
                starts_branch = re.match(
                    r'(\}\s*)?(else\b|(else\s+)?(if|while|for)\s*\()', head)
                if "\n" in prefix and starts_branch and "{" not in prefix:
                    nl = prefix.rindex("\n")
                    body_indent = re.match(r'\s*', line[nl + 1:]).group()
                    return (line[:nl + 1] + body_indent
                            + ";  // removed call to " + name + "()")
                return indent + "// removed call to " + name + "()"
            # Call embedded in a larger expression -> drop just this call.
            new_line = line[:start] + line[end:]
            if new_line.strip():
                return new_line.rstrip() + "  // removed call to " + name + "()"
            return indent + "// removed call to " + name + "()"
        # Call not locatable on this line (e.g. it spans multiple lines): defer
        # to the multiline/neighbour-line fallback instead of emitting a
        # comment-only no-op mutant.
        return line + " // MUTATED: " + d

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
        elif (not val.endswith(")") and not val.endswith(";")
              and re.match(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+$', val)):
            # A dotted *name* (e.g. Collections.emptyList) needs invoking; but a
            # numeric literal such as 0.0d / 0.0f also contains '.' and must not
            # get "()" appended.
            val += "()"
            # PIT names java.util helpers unqualified (`Collections.emptyList`),
            # but the source file may not import them -> use the fully-qualified
            # name so the spliced return compiles regardless of imports.
            if val.startswith("Collections."):
                val = "java.util." + val
        # Replace the whole `return <expr>;` statement. Find the statement's real
        # terminating ';' (ignoring ';' inside string literals or nested parens)
        # instead of a non-greedy regex that stops at the first ';', which could
        # be inside a string like `endsWith(">;")`.
        rm = re.search(r'\breturn\b', line)
        if rm:
            semi = _toplevel_semicolon(line, rm.end())
            if semi != -1:
                return line[:rm.start()] + 'return ' + val + ';' + line[semi + 1:]
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
    # When the mutated line is itself a continuation of a multi-line condition
    # (`&& property.equals(...)` under a wrapped `if (...)`), a full-line comment
    # between continuation lines must not stop the backward walk -- otherwise the
    # statement start (the `if (`) is never reached and the span runs to the end
    # of the method, corrupting the file ("class, interface, or enum expected").
    _cont_starts = ('&&', '||', '?', ':', '.', ')', ',')
    _target_is_continuation = lines[target - 1].lstrip().startswith(_cont_starts)
    while s > 1:
        raw = lines[s - 2]
        prev = re.sub(r'//[^\n]*$', '', raw).rstrip()
        if not prev:
            # A comment-only line (blank after stripping `//`, but non-blank in
            # the source) inside a multi-line condition is not a boundary.
            if _target_is_continuation and raw.strip():
                s -= 1
                continue
            break
        if prev[-1] in (';', '{', '}'):
            break
        # Do not walk across a Javadoc/block comment or an annotation into a
        # preceding declaration (e.g. a single-line method under @Override + Javadoc).
        if prev.endswith('*/'):
            break
        stripped = prev.lstrip()
        if stripped.startswith(('@', '*', '/*')):
            break
        s -= 1
    paren = brack = brace = 0
    # A `return`/`throw` statement is an *expression* statement: any `{` in it
    # opens an anonymous class or array initializer that is part of the
    # statement (e.g. `return new ClassFile() { ... };`), so it must not
    # terminate the span -- only the trailing `;` does. Track brace depth and
    # descend through such braces instead of stopping at the first `{`.
    expr_stmt = bool(re.match(r'\s*(return|throw)\b', lines[s - 1]))
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
            elif c == ';' and paren == 0 and brack == 0 and brace == 0:
                return s, i + 1
            elif c == '{' and paren == 0 and brack == 0:
                if expr_stmt:
                    brace += 1
                else:
                    return s, i + 1
            elif c == '}' and paren == 0 and brack == 0 and brace > 0:
                brace -= 1
            j += 1
    return s, n


class _AdjTok:
    __slots__ = ('value', 'position')

    def __init__(self, value, position):
        self.value = value
        self.position = position


def apply_mutation_multiline(lines, stmt_tokens, desc, occ, stmt_start, stmt_end,
                             mut_line=None):
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

    fallback_marker = "// MUTATED: " + desc.strip()

    # PIT's occurrence index is per source LINE, but the joined statement may
    # span several lines each carrying their own conditionals (a wrapped
    # `if (a && b && c.equals(...))`). Selecting the occ-th candidate across the
    # whole statement would fold the wrong sub-expression (line 96's `a` instead
    # of line 98's `c.equals`). Translate the per-line occ into the global occ by
    # walking global candidates and keeping only those whose fold lands on the
    # mutation line's slice of the joined string.
    if mut_line is not None and stmt_start <= mut_line <= stmt_end and stmt_end > stmt_start:
        rel = mut_line - stmt_start
        win_lo = line_starts[rel]
        win_hi = line_starts[rel + 1] if rel + 1 < len(line_starts) else len(joined) + 1
        seen = 0
        for g in range(len(adjusted) + 4):
            out = apply_mutation(joined, adjusted, desc, g)
            if out is None or out.endswith(fallback_marker) or out.strip() == joined.strip():
                break
            cs = next((k for k in range(min(len(joined), len(out))) if joined[k] != out[k]),
                      min(len(joined), len(out)))
            if win_lo <= cs < win_hi:
                if seen == occ:
                    return (stmt_start, stmt_end, out)
                seen += 1

    result = apply_mutation(joined, adjusted, desc, occ)

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
        ml = apply_mutation_multiline(lines, stmt_tokens, desc, occ, s, e,
                                      mut_line=lineno)
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
    """Yield mutation records (with method-body context) for the dataset."""
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
        if not dbg_skips:
            return
        row = f"({rel_src}:{lineno}) mutator={mut.findtext('mutator','')} index={bc_index} desc={desc!r}"
        print(f"{xml_line_of.get(id(mut), '?')}\t{row}\treason: {reason}", file=sys.stderr)

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
                    lines, tokens, lineno, desc, occ,
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
    for info in iter_mutations(system_path):
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

    methods_csv = os.path.join(dataset_dir, "mutated_methods.csv")
    meta_csv = os.path.join(dataset_dir, "meta.csv")
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
    print(f"Wrote {methods_csv} and {meta_csv} ({len(methods_rows)} rows)")


if __name__ == "__main__":
    main()
