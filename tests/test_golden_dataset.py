"""Golden / characterization regression: replay every committed dataset.

This is the safety net for engine changes. It re-runs the real reconstruction over
each test-project's `mutations.xml` and asserts the output still matches the CSVs
committed under `test-projects/<p>/PITMuS_dataset/`.

Any behavioral drift in statement location, mutator application, method-body
extraction or test-provenance shows up here as a concrete row diff.

Scope control:
  * projects whose inputs are absent are skipped (works in a bare clone)
  * only the fast projects run by default; the rest need `--runslow`

Refreshing the baseline (only after you have *reviewed* the diff and concluded the
new output is correct):

    python PITMuS/gen_dataset.py test-projects/<project>
"""

import csv
import sys

import pytest

from conftest import TOOL_DIR

sys.path.insert(0, str(TOOL_DIR))
csv.field_size_limit(10**9)

import gen_dataset  # noqa: E402

# meta CSV column -> key yielded by iter_mutations()
META_COLUMNS = {
    "mutation_line": "original_line",
    "mutated_line": "mutated_line",
    "source_file": "source_file",
    "stmt_start_line": "stmt_start_line",
    "pit_line_number": "pit_line_number",
    "description": "description",
    "test_file": "test_files",
    "index_no": "index_no",
    "xml_line": "xml_line",
}

METHOD_COLUMNS = {
    "original_method": "original_method",
    "mutated_method": "mutated_method_body",
    "docstring": "docstring",
}

# Anything larger than this only runs under --runslow (full sweep is ~2 min).
FAST_ROW_BUDGET = 1500


def _projects(repo_root):
    root = repo_root / "test-projects"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _paths(repo_root, project):
    proj = repo_root / "test-projects" / project
    return {
        "dir": proj,
        "xml": proj / "target" / "pit-reports" / "mutations.xml",
        "src": proj / "src" / "main" / "java",
        "meta": proj / "PITMuS_dataset" / f"meta-{project}.csv",
        "methods": proj / "PITMuS_dataset" / f"mutated_methods-{project}.csv",
    }


def _load_baseline(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


_RECONSTRUCTION_CACHE = {}


def reconstruct(project_dir, with_skips=False):
    """Reconstruct once per project and reuse; the sweep is the slow part."""
    key = str(project_dir)
    if key not in _RECONSTRUCTION_CACHE:
        skips = []
        rows = list(gen_dataset.iter_mutations(key, skips))
        _RECONSTRUCTION_CACHE[key] = (rows, skips)
    rows, skips = _RECONSTRUCTION_CACHE[key]
    return (rows, skips) if with_skips else rows


_DRIFT_STARTED = set()


def drift_path(repo_root, project):
    return (
        repo_root
        / "evaluation"
        / "evaluation_results"
        / f"{project}_results"
        / f"golden-drift-{project}.txt"
    )


def _record_drift(repo_root, project, ids):
    """Write the index_no values that drifted from the committed baseline.

    The evaluation notebook reads this file and restricts eval2/eval3 to just these
    rows, so accepting a change costs a handful of compiles instead of a full sweep.
    Rewritten from scratch once per session, then merged across the tests in it; the
    file is removed when nothing drifted, so a stale list can never mislead eval.
    """
    path = drift_path(repo_root, project)
    existing = set()
    if project in _DRIFT_STARTED and path.exists():
        existing = {
            ln.strip()
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        }
    _DRIFT_STARTED.add(project)

    merged = sorted(existing | {str(i) for i in ids}, key=int)
    if not merged:
        if path.exists():
            path.unlink()
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {project}: {len(merged)} row(s) differ from the committed dataset.\n"
        "# Written by tests/test_golden_dataset.py; consumed by "
        "evaluation/evaluate_reconstruction.ipynb (DRIFT_ONLY).\n"
        "# One index_no per line.\n" + "\n".join(merged) + "\n",
        encoding="utf-8",
    )
    return path


def _require_inputs(paths, request):
    for key in ("xml", "src", "meta"):
        if not paths[key].exists():
            pytest.skip(f"missing {paths[key].name} -- nothing to compare against")
    baseline = _load_baseline(paths["meta"])
    if len(baseline) > FAST_ROW_BUDGET and not request.config.getoption("--runslow"):
        pytest.skip(f"{len(baseline)} rows; run with --runslow")
    return baseline


def pytest_generate_tests(metafunc):
    if "project" in metafunc.fixturenames:
        from conftest import REPO_ROOT

        metafunc.parametrize("project", _projects(REPO_ROOT))


def test_meta_rows_match_committed_dataset(project, repo_root, request):
    paths = _paths(repo_root, project)
    baseline = _require_inputs(paths, request)

    produced = reconstruct(paths["dir"])

    assert len(produced) == len(baseline), (
        f"{project}: reconstructed {len(produced)} mutations but the committed "
        f"dataset has {len(baseline)}. A mutation started or stopped being reconstructed."
    )

    diffs = []
    drifted = set()
    for got, want in zip(produced, baseline):
        for column, key in META_COLUMNS.items():
            if str(got[key]) != want[column]:
                drifted.add(got["index_no"])
                diffs.append(
                    f"  row index_no={want['index_no']} ({want['source_file']}:"
                    f"{want['pit_line_number']}) column={column}\n"
                    f"    committed: {want[column]!r}\n"
                    f"    produced : {str(got[key])!r}"
                )
    _record_drift(repo_root, project, drifted)
    assert not diffs, (
        f"{project}: {len(diffs)} column diff(s) vs the committed dataset.\n"
        + "\n".join(diffs[:10])
        + (f"\n  ... and {len(diffs) - 10} more" if len(diffs) > 10 else "")
    )


def test_method_bodies_match_committed_dataset(project, repo_root, request):
    paths = _paths(repo_root, project)
    _require_inputs(paths, request)
    if not paths["methods"].exists():
        pytest.skip("no mutated_methods CSV committed")

    baseline = _load_baseline(paths["methods"])
    produced = reconstruct(paths["dir"])
    assert len(produced) == len(baseline)

    diffs = []
    drifted = set()
    for got, want in zip(produced, baseline):
        for column, key in METHOD_COLUMNS.items():
            if str(got[key]) != want[column]:
                drifted.add(got["index_no"])
                diffs.append(f"  index_no={want['index_no']} column={column}")
    _record_drift(repo_root, project, drifted)
    assert not diffs, f"{project}: {len(diffs)} method-body diff(s)\n" + "\n".join(diffs[:10])


# ------------------------------------------------------- structural invariants
# These hold for any healthy dataset, independent of the committed baseline.


def test_dataset_invariants(project, repo_root, request):
    paths = _paths(repo_root, project)
    _require_inputs(paths, request)
    produced = reconstruct(paths["dir"])

    seen_ids = set()
    for row in produced:
        idx = row["index_no"]
        assert idx not in seen_ids, f"duplicate index_no {idx}"
        seen_ids.add(idx)

        assert row["original_line"] != row["mutated_line"], (
            f"index_no={idx}: mutation produced no textual change"
        )
        assert " // MUTATED: " not in row["mutated_line"], (
            f"index_no={idx}: unapplied fallback marker leaked into the dataset"
        )
        assert row["original_method"] != row["mutated_method_body"], (
            f"index_no={idx}: method body unchanged after mutation"
        )
        assert row["original_method"].strip(), f"index_no={idx}: empty original method"
        assert int(row["pit_line_number"]) > 0
        assert int(row["stmt_start_line"]) > 0
        assert row["source_file"].endswith(".java")


def _scan_java(text):
    """Walk `text` as Java, returning bracket depths and whether it ends inside a
    comment or literal. Comments and string/char literals are skipped, so braces
    and parens inside them do not count."""
    paren = brack = brace = 0
    in_str = in_char = in_block = False
    code = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            i += 1
        elif in_str or in_char:
            code.append(c)
            if c == "\\":
                code.append(nxt)
                i += 2
                continue
            if (in_str and c == '"') or (in_char and c == "'"):
                in_str = in_char = False
            i += 1
        elif c == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and nxt == "*":
            in_block = True
            i += 2
        else:
            if c == '"':
                in_str = True
            elif c == "'":
                in_char = True
            elif c == "(":
                paren += 1
            elif c == ")":
                paren -= 1
            elif c == "[":
                brack += 1
            elif c == "]":
                brack -= 1
            elif c == "{":
                brace += 1
            elif c == "}":
                brace -= 1
            code.append(c)
            i += 1
    tail = "".join(code).rstrip()
    return paren, brack, brace, in_block, tail[-1] if tail else ""


def _span_defects(text):
    """Reasons `text` is not a usable, self-contained span -- empty when it is fine.

    Two shapes are intentionally unbalanced and must be allowed:
      * `if (cond) {`     -- a control-structure header whose body is excluded on
                             purpose, because the mutation lives in the condition
      * `} while (cond);` -- the mirror image, the tail of a do-while

    Everything else must balance. A span that does not is the Issue-5 failure mode:
    it either ran past the end of its own construct or was cut mid-expression, and
    downstream consumers get Java that will not parse.
    """
    paren, brack, brace, in_block, last = _scan_java(text)
    defects = []
    if paren:
        defects.append(f"paren={paren:+d}")
    if brack:
        defects.append(f"bracket={brack:+d}")
    if in_block:
        defects.append("ends inside an unterminated /* comment")
    header = last == "{" and brace == 1
    tail = text.lstrip().startswith("}") and brace == -1
    if brace and not (header or tail):
        defects.append(f"brace={brace:+d}")

    # A span that opens with a binary operator is the tail of an expression that
    # began on an earlier line -- balanced, but still not a statement. This is how
    # the Code.java rows failed: the span started at `+ 8 * (...)`, mid-sum.
    head = text.lstrip()
    # `// removed call to foo()` is a whole-span comment, the shape the "removed
    # call" mutator emits -- not a fragment.
    if not head.startswith(("//", "/*", "++")):
        for op in ("==", "!=", "<=", ">=", "&&", "||", "<<", ">>", "->"):
            if head.startswith(op):
                defects.append(f"starts mid-expression with `{op}`")
                break
        else:
            if head[:1] in set("+*/%^,?:|&="):
                defects.append(f"starts mid-expression with `{head[:1]}`")
    return defects


def test_spans_are_self_contained(project, repo_root, request):
    """Every emitted span must be balanced Java.

    Guards the Issue-5 regression, where a trailing inline block comment made the
    backward walk stop mid-expression; the forward scan then ran 7,499 characters
    past the enclosing method, and a statement followed by an unclosed `/*` was cut
    before the comment ended. Both produced spans that would not compile.
    """
    paths = _paths(repo_root, project)
    _require_inputs(paths, request)

    bad = []
    for row in reconstruct(paths["dir"]):
        for key in ("original_line", "mutated_line"):
            defects = _span_defects(row[key])
            if defects:
                first = row[key].splitlines()[0].strip()[:60] if row[key] else ""
                bad.append(
                    f"  index_no={row['index_no']} {key}: {', '.join(defects)}"
                    f"\n      {row['source_file']}:{row['stmt_start_line']}  {first}"
                )
                break

    assert not bad, (
        f"{project}: {len(bad)} span(s) are not self-contained Java\n"
        + "\n".join(bad[:10])
        + (f"\n  ... and {len(bad) - 10} more" if len(bad) > 10 else "")
    )


def test_index_numbers_are_dense_and_one_based(project, repo_root, request):
    paths = _paths(repo_root, project)
    _require_inputs(paths, request)
    ids = [r["index_no"] for r in reconstruct(paths["dir"])]
    assert ids == list(range(1, len(ids) + 1))


def test_reconstruction_is_deterministic(project, repo_root, request):
    """Two runs over the same report must be identical -- ordering bugs here would
    silently reshuffle index_no across regenerations."""
    paths = _paths(repo_root, project)
    _require_inputs(paths, request)
    d = str(paths["dir"])
    first = [(r["index_no"], r["mutated_line"]) for r in gen_dataset.iter_mutations(d)]
    second = [(r["index_no"], r["mutated_line"]) for r in gen_dataset.iter_mutations(d)]
    assert first == second


def test_skips_are_reported_with_reasons(project, repo_root, request):
    """Every unreconstructed mutation must be accounted for, never dropped silently."""
    paths = _paths(repo_root, project)
    _require_inputs(paths, request)
    rows, skips = reconstruct(paths["dir"], with_skips=True)

    import xml.etree.ElementTree as ET

    total_in_xml = len(ET.parse(paths["xml"]).getroot().findall("mutation"))
    assert len(rows) + len(skips) == total_in_xml, (
        f"{project}: {total_in_xml} mutations in XML but {len(rows)} reconstructed "
        f"+ {len(skips)} skipped -- some were dropped without being recorded"
    )
    for _xml_line, _location, reason in skips:
        assert reason, "a skip was recorded without a reason"
