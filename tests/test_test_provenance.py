"""Test-provenance extraction across both PIT XML dialects.

PIT writes the killing/covering tests in one of two shapes depending on the
``fullMutationMatrix`` option:

    fullMutationMatrix=true : <killingTests>|<coveringTests>   (plural, '|'-lists)
    default (PIT 1.22)      : <killingTest> |<coveringTest>    (singular)

Every mutations.xml committed to test-projects/ happens to use the plural form, so
the singular dialect had no coverage at all and reading it returned nothing --
provenance was dropped silently, which is indistinguishable from a project whose
mutants genuinely have no covering test. These tests pin down both dialects and the
warning that makes the difference visible.
"""

import sys
import xml.etree.ElementTree as ET

import pytest

from conftest import SIMPLE_JAVA, TOOL_DIR, mutation_xml

sys.path.insert(0, str(TOOL_DIR))

from shared import collect_test_ids, detect_test_dialect  # noqa: E402

JUPITER = "[engine:junit-jupiter]/[class:org.example.{cls}]/[method:m()]"


def one_mutation(**fields):
    """A single <mutation> element carrying the given test fields."""
    base = {
        "sourceFile": "Foo.java", "mutatedClass": "com.example.Foo",
        "mutatedMethod": "add", "methodDescription": "(II)I", "lineNumber": 6,
        "mutator": "org.pitest.mutationtest.engine.gregor.mutators.MathMutator",
        "description": "Replaced integer addition with subtraction",
        "indexes": [4], "blocks": [0],
    }
    base.update(fields)
    return ET.fromstring(mutation_xml([base])).find("mutation")


# --------------------------------------------------------------- collect_test_ids

def test_singular_killing_element_is_read():
    """PIT 1.22 default dialect -- the case that was silently dropped."""
    mut = one_mutation(killingTest=JUPITER.format(cls="ArrayUtilsTest"))
    assert collect_test_ids(mut, "killing") == JUPITER.format(cls="ArrayUtilsTest")


def test_singular_covering_element_is_read():
    mut = one_mutation(coveringTest=JUPITER.format(cls="ArrayUtilsTest"))
    assert collect_test_ids(mut, "covering") == JUPITER.format(cls="ArrayUtilsTest")


def test_plural_element_still_works():
    """The legacy fullMutationMatrix dialect must keep behaving exactly as before."""
    mut = one_mutation(killingTests="testA(org.example.FooTest)")
    assert collect_test_ids(mut, "killing") == "testA(org.example.FooTest)"


def test_repeated_elements_are_all_collected():
    mut = one_mutation(killingTest=["a(org.example.ATest)", "b(org.example.BTest)"])
    assert collect_test_ids(mut, "killing") == "a(org.example.ATest)|b(org.example.BTest)"


def test_duplicates_are_dropped_and_first_seen_order_kept():
    mut = one_mutation(killingTest=["z(org.example.ZTest)", "a(org.example.ATest)",
                                    "z(org.example.ZTest)"])
    assert collect_test_ids(mut, "killing") == "z(org.example.ZTest)|a(org.example.ATest)"


def test_both_dialects_present_are_merged_without_duplication():
    mut = one_mutation(killingTests="shared|plural_only", killingTest="shared")
    assert collect_test_ids(mut, "killing") == "shared|plural_only"


def test_empty_elements_yield_an_empty_string():
    """A survived or uncovered mutation: elements exist but carry no test."""
    mut = one_mutation(killingTest="", coveringTest="")
    assert collect_test_ids(mut, "killing") == ""
    assert collect_test_ids(mut, "covering") == ""


def test_absent_elements_yield_an_empty_string():
    mut = one_mutation()
    assert collect_test_ids(mut, "killing") == ""
    assert collect_test_ids(mut, "covering") == ""


def test_whitespace_only_values_are_not_treated_as_tests():
    mut = one_mutation(killingTest="   ")
    assert collect_test_ids(mut, "killing") == ""


def test_killing_and_covering_are_kept_separate():
    mut = one_mutation(killingTest="k(org.example.KTest)",
                       coveringTest="c(org.example.CTest)")
    assert collect_test_ids(mut, "killing") == "k(org.example.KTest)"
    assert collect_test_ids(mut, "covering") == "c(org.example.CTest)"


# ------------------------------------------------------------ detect_test_dialect

@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"killingTests": "x"}, "matrix"),
        ({"coveringTests": ""}, "matrix"),
        ({"succeedingTests": ""}, "matrix"),
        ({"killingTest": "x"}, "single"),
        ({"coveringTest": ""}, "single"),
        ({"killingTest": "x", "coveringTests": "y"}, "mixed"),
        ({}, "none"),
    ],
)
def test_detect_dialect(tmp_path, fields, expected):
    path = tmp_path / "mutations.xml"
    path.write_text(mutation_xml([dict({"sourceFile": "Foo.java"}, **fields)]),
                    encoding="utf-8")
    assert detect_test_dialect(str(path)) == expected


def test_detect_dialect_on_a_missing_file_is_none():
    assert detect_test_dialect("/definitely/not/here.xml") == "none"


def test_detect_dialect_matches_the_committed_projects(repo_root):
    """The committed reports were all produced with fullMutationMatrix=true."""
    xml = repo_root / "test-projects" / "commons-dbutils" / "target" / "pit-reports" / "mutations.xml"
    if not xml.exists():
        pytest.skip("commons-dbutils inputs not present")
    assert detect_test_dialect(str(xml)) == "matrix"


# ------------------------------------------------------------------ CLI behaviour

def meta_test_files(run_cli, fake_project, muts):
    import csv

    out = run_cli(fake_project(SIMPLE_JAVA, muts, name="fakeproj"))
    with open(out["meta"], newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r["test_file"] for r in rows]


def add_mutation(**fields):
    base = {
        "sourceFile": "Foo.java", "mutatedClass": "com.example.Foo",
        "mutatedMethod": "add", "methodDescription": "(II)I", "lineNumber": 6,
        "mutator": "org.pitest.mutationtest.engine.gregor.mutators.MathMutator",
        "description": "Replaced integer addition with subtraction",
        "indexes": [4], "blocks": [0],
    }
    base.update(fields)
    return base


def test_dataset_keeps_provenance_from_a_singular_report(fake_project, run_cli):
    """End-to-end version of the reported bug: a default PIT 1.22 report."""
    muts = [add_mutation(killingTest=JUPITER.format(cls="ArrayUtilsTest"))]
    assert meta_test_files(run_cli, fake_project, muts) == ["ArrayUtilsTest.java"]


def test_dataset_keeps_provenance_from_a_plural_report(fake_project, run_cli):
    muts = [add_mutation(coveringTests="testAdd(org.example.ArrayUtilsTest)")]
    assert meta_test_files(run_cli, fake_project, muts) == ["ArrayUtilsTest.java"]


def test_covering_takes_precedence_over_killing(fake_project, run_cli):
    """Documented precedence: covering is the broader set, killing is the fallback."""
    muts = [add_mutation(coveringTest=JUPITER.format(cls="CoveringTest"),
                         killingTest=JUPITER.format(cls="KillingTest"))]
    assert meta_test_files(run_cli, fake_project, muts) == ["CoveringTest.java"]


def test_killing_is_used_when_covering_is_empty(fake_project, run_cli):
    muts = [add_mutation(coveringTest="", killingTest=JUPITER.format(cls="KillingTest"))]
    assert meta_test_files(run_cli, fake_project, muts) == ["KillingTest.java"]


def test_uncovered_mutation_has_no_test_file(fake_project, run_cli):
    """NO_COVERAGE: valid, and must stay distinguishable from a parse failure."""
    muts = [add_mutation(killingTest="", coveringTest="")]
    assert meta_test_files(run_cli, fake_project, muts) == [""]


def test_run_reports_how_many_rows_carry_provenance(fake_project, run_cli, capsys):
    muts = [add_mutation(killingTest=JUPITER.format(cls="ArrayUtilsTest"))]
    meta_test_files(run_cli, fake_project, muts)
    assert "Test provenance: 1/1 rows (XML dialect: single)" in capsys.readouterr().out


def test_run_warns_when_test_elements_exist_but_nothing_resolves(fake_project, run_cli, capsys):
    """The audit the issue asks for: a schema we recognize but cannot read."""
    muts = [add_mutation(killingTest="[engine:junit-jupiter]/[method:m(java.lang.String)]")]
    assert meta_test_files(run_cli, fake_project, muts) == [""]
    assert "no row resolved a test file" in capsys.readouterr().err


def test_no_warning_when_the_report_simply_has_no_tests(fake_project, run_cli, capsys):
    """A report with no test elements is not a fault -- it must stay quiet."""
    meta_test_files(run_cli, fake_project, [add_mutation()])
    captured = capsys.readouterr()
    assert "Warning" not in captured.err
    assert "XML dialect: none" in captured.out
