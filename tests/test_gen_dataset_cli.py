"""Tests for the gen_dataset.py CLI: CSV emission and the skip bookkeeping.

The golden-dataset tests call `iter_mutations()` directly, so the *data* is well
covered but `main()` -- which decides the CSV filenames, headers and column order --
is not. A column reorder or a renamed header would sail past every other test while
silently invalidating every downstream consumer (the eval notebook reads these CSVs
by name). That is what this file pins down, along with the skip paths that no
committed project happens to exercise.
"""

import csv
import shutil
import sys

import pytest

from conftest import SIMPLE_JAVA, TOOL_DIR, simple_mutations

sys.path.insert(0, str(TOOL_DIR))

import gen_dataset  # noqa: E402

csv.field_size_limit(10**9)

META_HEADER = [
    "mutation_line", "mutated_line", "source_file", "stmt_start_line",
    "pit_line_number", "description", "test_file", "index_no", "xml_line",
]
METHODS_HEADER = ["index_no", "original_method", "mutated_method", "docstring"]


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        return next(reader), list(reader)


# ----------------------------------------------------------------- CSV emission

def test_main_writes_both_csvs_named_after_the_project(fake_project, run_cli):
    out = run_cli(fake_project(SIMPLE_JAVA, simple_mutations(), name="fakeproj"))
    assert out["meta"].exists()
    assert out["methods"].exists()


def test_meta_csv_header_and_column_order_are_stable(fake_project, run_cli):
    """The eval notebook indexes these columns by name -- renaming one breaks it."""
    out = run_cli(fake_project(SIMPLE_JAVA, simple_mutations(), name="fakeproj"))
    header, rows = read_rows(out["meta"])
    assert header == META_HEADER
    assert len(rows) == 2
    assert all(len(r) == len(META_HEADER) for r in rows)


def test_methods_csv_header_and_column_order_are_stable(fake_project, run_cli):
    out = run_cli(fake_project(SIMPLE_JAVA, simple_mutations(), name="fakeproj"))
    header, rows = read_rows(out["methods"])
    assert header == METHODS_HEADER
    assert len(rows) == 2


def test_the_two_csvs_agree_row_for_row_on_index_no(fake_project, run_cli):
    out = run_cli(fake_project(SIMPLE_JAVA, simple_mutations(), name="fakeproj"))
    meta_header, meta_rows = read_rows(out["meta"])
    _, method_rows = read_rows(out["methods"])
    meta_ids = [r[meta_header.index("index_no")] for r in meta_rows]
    assert meta_ids == [r[0] for r in method_rows] == ["1", "2"]


def test_meta_rows_carry_the_reconstructed_mutation(fake_project, run_cli):
    out = run_cli(fake_project(SIMPLE_JAVA, simple_mutations(), name="fakeproj"))
    header, rows = read_rows(out["meta"])
    row = dict(zip(header, rows[0]))
    assert row["mutation_line"] == "return a + b;"
    assert row["mutated_line"] == "return a - b;"
    assert row["source_file"] == "com/example/Foo.java"
    assert row["pit_line_number"] == "6"


def test_test_files_column_resolves_junit_platform_identifiers(fake_project, run_cli):
    """Guards the [class:...] fix end-to-end, not just at the helper level."""
    out = run_cli(fake_project(SIMPLE_JAVA, simple_mutations(), name="fakeproj"))
    header, rows = read_rows(out["meta"])
    assert {dict(zip(header, r))["test_file"] for r in rows} == {"FooTest.java"}


def test_docstring_column_captures_the_javadoc(fake_project, run_cli):
    out = run_cli(fake_project(SIMPLE_JAVA, simple_mutations(), name="fakeproj"))
    _, rows = read_rows(out["methods"])
    assert "Adds two numbers" in rows[0][3]


def test_xml_line_points_at_the_mutation_in_mutations_xml(fake_project, run_cli):
    """Rows must be traceable back to mutations.xml; the mapping assumes one
    <mutation> per physical line, so a serializer change would break it silently."""
    proj = fake_project(SIMPLE_JAVA, simple_mutations(), name="fakeproj")
    out = run_cli(proj)
    header, rows = read_rows(out["meta"])
    xml_lines = (proj / "target" / "pit-reports" / "mutations.xml").read_text().splitlines()
    for row in rows:
        recorded = int(dict(zip(header, row))["xml_line"])
        assert "<mutation " in xml_lines[recorded - 1]


# ------------------------------------------------------------------ skip report

def test_skip_report_is_written_even_when_nothing_is_skipped(fake_project, run_cli):
    out = run_cli(fake_project(SIMPLE_JAVA, simple_mutations(), name="fakeproj"))
    assert out["skips"].exists()
    assert "0 of 2 mutation(s) skipped" in out["skips"].read_text(encoding="utf-8")


def test_out_of_range_line_number_is_skipped_with_a_reason(fake_project, run_cli):
    muts = simple_mutations()
    muts[0]["lineNumber"] = 9999
    out = run_cli(fake_project(SIMPLE_JAVA, muts, name="fakeproj"))

    _, rows = read_rows(out["meta"])
    assert len(rows) == 1, "the bad mutation should not reach the dataset"
    assert "out of source-file range" in out["skips"].read_text(encoding="utf-8")


def test_unapplied_mutator_is_skipped_rather_than_emitted(fake_project, run_cli):
    """A description whose pattern is absent from the line must be dropped, never
    written out with the ' // MUTATED: ' fallback marker still attached."""
    muts = simple_mutations()
    muts[1]["description"] = "Replaced integer addition with subtraction"  # line has no '+'
    out = run_cli(fake_project(SIMPLE_JAVA, muts, name="fakeproj"))

    _, rows = read_rows(out["meta"])
    assert len(rows) == 1
    text = out["skips"].read_text(encoding="utf-8")
    assert "mutation not applied" in text
    assert "1 of 2 mutation(s) skipped" in text


def test_every_mutation_is_either_reconstructed_or_skipped(fake_project, run_cli):
    muts = simple_mutations()
    muts[0]["lineNumber"] = 9999
    out = run_cli(fake_project(SIMPLE_JAVA, muts, name="fakeproj"))

    _, rows = read_rows(out["meta"])
    skips = [
        ln for ln in out["skips"].read_text(encoding="utf-8").splitlines()
        if ln and not ln.startswith("#")
    ]
    assert len(rows) + len(skips) == len(muts)


def test_skip_debug_env_var_echoes_reasons_to_stderr(fake_project, run_cli, monkeypatch, capsys):
    monkeypatch.setenv("PITMUS_DEBUG_SKIPS", "1")
    muts = simple_mutations()
    muts[0]["lineNumber"] = 9999
    run_cli(fake_project(SIMPLE_JAVA, muts, name="fakeproj"))
    assert "reason:" in capsys.readouterr().err


# ------------------------------------------------------------- source resolution

FIELD_JAVA = """package com.example;

public class Bar {
    public static final int N = 1 + 2;
}
"""


def test_mutation_in_a_field_initializer_is_still_reconstructed(fake_project, run_cli):
    """Field initializers sit outside every method span; they must fall back to the
    statement itself rather than being dropped for having no enclosing method."""
    muts = [{
        "sourceFile": "Bar.java", "mutatedClass": "com.example.Bar",
        "mutatedMethod": "<clinit>", "methodDescription": "()V", "lineNumber": 4,
        "mutator": "org.pitest.mutationtest.engine.gregor.mutators.MathMutator",
        "description": "Replaced integer addition with subtraction",
        "indexes": [3], "blocks": [0],
    }]
    proj = fake_project(FIELD_JAVA, muts, class_name="Bar", name="fakeproj")
    header, rows = read_rows(run_cli(proj)["meta"])
    assert len(rows) == 1
    assert "1 - 2" in dict(zip(header, rows[0]))["mutated_line"]


def test_sources_under_generated_sources_are_found(fake_project, run_cli):
    """Generated sources (jjtree/annotations) do not live under src/main/java."""
    proj = fake_project(SIMPLE_JAVA, simple_mutations(), name="fakeproj")
    src = proj / "src" / "main" / "java" / "com" / "example" / "Foo.java"
    generated = proj / "target" / "generated-sources" / "java" / "com" / "example"
    generated.mkdir(parents=True)
    shutil.move(str(src), str(generated / "Foo.java"))

    _, rows = read_rows(run_cli(proj)["meta"])
    assert len(rows) == 2, "generated sources should still be reconstructed"


def test_missing_source_file_is_skipped_not_crashed(fake_project, run_cli):
    proj = fake_project(SIMPLE_JAVA, simple_mutations(), name="fakeproj")
    (proj / "src" / "main" / "java" / "com" / "example" / "Foo.java").unlink()

    out = run_cli(proj)
    _, rows = read_rows(out["meta"])
    assert rows == []
    assert out["skips"].exists()


SWITCH_JAVA = """package com.example;

public class Sw {
    public int pick(int n) {
        switch (n) {
            case 1: return 10;
            case 2: return 20;
            default: return 0;
        }
    }
}
"""


def switch_mutations():
    return [
        {
            "sourceFile": "Sw.java", "mutatedClass": "com.example.Sw",
            "mutatedMethod": "pick", "methodDescription": "(I)I", "lineNumber": line,
            "mutator": "org.pitest.mutationtest.engine.gregor.mutators.SwitchMutator",
            "description": "Changed switch default to be the first case",
            "indexes": [index], "blocks": [0],
        }
        for line, index in ((5, 2), (5, 6))
    ]


def test_switch_default_degrades_to_an_annotation_without_compiled_classes(fake_project, run_cli):
    """CHARACTERIZATION -- documents current degraded behavior, not desired behavior.

    Rewriting a `Changed switch default` mutation needs the switch tables, which come
    from javap over target/classes. With no compiled classes the engine cannot rewrite
    the switch, so it annotates the `switch (...)` line with a trailing comment
    instead. The row is still emitted, and two *distinct* PIT mutations collapse to
    byte-identical output -- so switch mutants from a project without target/classes
    are not usable as mutants. Run PIT's build first if you need them.

    Worth knowing before "fixing" this: the annotated row differs from the original by
    a comment alone, so it compiles to identical bytecode and no test can kill it, and
    it slips past the `// MUTATED:` fallback-marker check below. Skipping such rows is
    the obvious repair, but `index_no` is a dense counter -- dropping rows renumbers
    every row after them and invalidates the committed datasets wholesale. Restore the
    build first, then change this, so the resulting diff is small enough to review.
    """
    out = run_cli(fake_project(SWITCH_JAVA, switch_mutations(), class_name="Sw", name="fakeproj"))
    header, rows = read_rows(out["meta"])
    mutated = [dict(zip(header, r))["mutated_line"] for r in rows]

    assert len(rows) == 2
    assert all(m.endswith("// switch default changed to first case") for m in mutated)
    assert mutated[0] == mutated[1], "expected the known collapse of distinct mutations"


# --------------------------------------------------------------- method spans

# Java 14 switch expression: javalang cannot parse this file at all, so the whole
# file used to yield zero method spans and every row fell back to `<field>`.
MODERN_JAVA = """package com.example;

public class Foo {
    /** Adds two numbers. */
    public int add(int a, int b) {
        return a + b;
    }

    public int add(int a, int b, int c) {
        return a + b + c;
    }

    public String describe(int day) {
        return switch (day) {
            case 1, 7 -> "weekend";
            default -> "weekday";
        };
    }
}
"""


def modern_mutation():
    return [{
        "sourceFile": "Foo.java", "mutatedClass": "com.example.Foo",
        "mutatedMethod": "add", "methodDescription": "(II)I", "lineNumber": 6,
        "mutator": "org.pitest.mutationtest.engine.gregor.mutators.MathMutator",
        "description": "Replaced integer addition with subtraction",
        "indexes": [4], "blocks": [0],
    }]


def test_unparseable_file_still_yields_the_enclosing_method(fake_project, run_cli):
    """The lexical fallback must recover the method body, not degrade to one line."""
    out = run_cli(fake_project(MODERN_JAVA, modern_mutation(), name="fakeproj"))
    _, rows = read_rows(out["methods"])
    assert len(rows) == 1
    original = rows[0][1]
    assert original.lstrip().startswith("public int add(int a, int b) {")
    assert original.rstrip().endswith("}")
    assert "return a + b;" in original


def test_a_mutation_in_an_overload_keeps_exactly_that_overload(fake_project, run_cli):
    """Adjacent overloads must not be merged into one span; `original_method` has to
    hold the two-argument `add` and none of the three-argument one."""
    out = run_cli(fake_project(MODERN_JAVA, modern_mutation(), name="fakeproj"))
    _, rows = read_rows(out["methods"])
    original, mutated = rows[0][1], rows[0][2]
    assert original.count("public int add(") == 1
    assert "return a + b + c;" not in original
    assert "return a - b;" in mutated
    assert "return a + b;" not in mutated


def test_span_report_counts_ast_lexical_and_field_rows(fake_project, run_cli, capsys):
    run_cli(fake_project(SIMPLE_JAVA, simple_mutations(), name="fakeproj"))
    out = capsys.readouterr().out
    assert "Method spans: ast=2  lexical=0  field=0  skipped=0" in out


def test_span_report_counts_lexical_rows_and_warns(fake_project, run_cli, capsys):
    run_cli(fake_project(MODERN_JAVA, modern_mutation(), name="fakeproj"))
    captured = capsys.readouterr()
    assert "lexical=1" in captured.out
    assert "lexical span fallback" in captured.err


def test_span_report_counts_field_rows(fake_project, run_cli, capsys):
    """A genuine field initializer must keep using the field fallback, not be skipped."""
    muts = [{
        "sourceFile": "Bar.java", "mutatedClass": "com.example.Bar",
        "mutatedMethod": "<clinit>", "methodDescription": "()V", "lineNumber": 4,
        "mutator": "org.pitest.mutationtest.engine.gregor.mutators.MathMutator",
        "description": "Replaced integer addition with subtraction",
        "indexes": [3], "blocks": [0],
    }]
    run_cli(fake_project(FIELD_JAVA, muts, class_name="Bar", name="fakeproj"))
    assert "ast=0  lexical=0  field=1  skipped=0" in capsys.readouterr().out


UNRECOVERABLE_JAVA = """package com.example;

public class Foo {
    ((( int a = 1 + 2;
"""


def test_unrecoverable_source_is_skipped_rather_than_labelled_field(fake_project, run_cli):
    """When neither the parser nor the lexical scan can see any method, the row must
    be dropped with a reason -- calling an in-method statement `<field>` would put a
    single line in `original_method` and silently corrupt the dataset."""
    muts = modern_mutation()
    muts[0]["lineNumber"] = 4
    out = run_cli(fake_project(UNRECOVERABLE_JAVA, muts, name="fakeproj"))

    _, rows = read_rows(out["meta"])
    assert rows == []
    assert "no method spans recovered" in out["skips"].read_text(encoding="utf-8")


# ------------------------------------------------------------- bad invocations

def run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["gen_dataset.py"] + argv)
    with pytest.raises(SystemExit) as exc:
        gen_dataset.main()
    return exc.value.code


@pytest.mark.parametrize(
    "argv_tail, expected",
    [
        ([], "Usage"),
        (["/definitely/not/a/dir"], "is not a directory"),
    ],
)
def test_main_rejects_bad_invocations(monkeypatch, capsys, argv_tail, expected):
    assert run_main(monkeypatch, argv_tail) == 1
    assert expected in capsys.readouterr().out


def test_main_reports_a_missing_pit_report(tmp_path, monkeypatch, capsys):
    assert run_main(monkeypatch, [str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "mutations.xml" in out and "not found" in out
