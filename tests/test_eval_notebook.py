"""Tests for the javap parser inside evaluation/evaluate_reconstruction.ipynb.

`_all_methods_text()` is the lookup table the eval3 bytecode oracle compares against.
It lives in a notebook, so nothing imports it and nothing tested it -- a silent parser
bug there does not fail anything, it just turns real verdicts into abstentions. That is
exactly what happened: every lambda method was keyed under a bare digit, so eval3 could
not verify a single mutation inside a lambda body on any project.

These tests lift the function straight out of the notebook cell and exercise it against
recorded `javap -c -p -s` output, so the notebook can no longer drift untested.
"""

import ast
import json
import re
import sys

import pytest

from conftest import REPO_ROOT

NOTEBOOK = REPO_ROOT / "evaluation" / "evaluate_reconstruction.ipynb"


def _load_from_notebook(func_name):
    """Compile a single top-level function out of the notebook it is defined in.

    The cell also contains statements that need the notebook's runtime state, so only
    the FunctionDef node is compiled -- never the whole cell.
    """
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    for cell in nb["cells"]:
        if cell.get("cell_type") != "code":
            continue
        src = "".join(cell["source"])
        if f"def {func_name}(" not in src:
            continue
        tree = ast.parse(src)
        node = next((n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == func_name), None)
        if node is None:
            continue
        ns = {"re": re}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(NOTEBOOK), "exec"), ns)
        return ns[func_name]
    raise AssertionError(f"{func_name} not found in {NOTEBOOK.name}")


@pytest.fixture(scope="module")
def parse():
    return _load_from_notebook("_all_methods_text")


# Recorded `javap -c -p -s` output. The lambda section is copied verbatim from PIT's own
# exported mutant of org.apache.bcel.util.ClassPath; the rest is trimmed to shape.
JAVAP = """Compiled from "ClassPath.java"
public class org.apache.bcel.util.ClassPath implements java.io.Closeable {
  public org.apache.bcel.util.ClassPath(java.lang.String);
    descriptor: (Ljava/lang/String;)V
    Code:
       0: aload_0
       1: invokespecial #1                  // Method java/lang/Object."<init>":()V
       4: return

  public java.lang.String getPath();
    descriptor: ()Ljava/lang/String;
    Code:
       0: aload_0
       1: getfield      #2                  // Field classPath:Ljava/lang/String;
       4: areturn

  private static boolean lambda$static$0(java.io.File, java.lang.String);
    descriptor: (Ljava/io/File;Ljava/lang/String;)Z
    Code:
       0: aload_1
       1: getstatic     #442                // Field java/util/Locale.ENGLISH:Ljava/util/Locale;
       4: invokevirtual #446                // Method java/lang/String.toLowerCase:(Ljava/util/Locale;)Ljava/lang/String;
       7: astore_1
       8: iconst_1
       9: ireturn

  private static boolean lambda$static$1(java.io.File, java.lang.String);
    descriptor: (Ljava/io/File;Ljava/lang/String;)Z
    Code:
       0: iconst_0
       1: ireturn
}
"""

# Eclipse's compiler names lambdas `lambda$0` instead of javac's `lambda$<method>$<n>`;
# BCEL's own target/classes is built that way, so both shapes occur in practice.
JAVAP_ECJ = """Compiled from "ClassPath.java"
public class org.apache.bcel.util.ClassPath implements java.io.Closeable {
  private static boolean lambda$0(java.io.File, java.lang.String);
    descriptor: (Ljava/io/File;Ljava/lang/String;)Z
    Code:
       0: iconst_1
       1: ireturn
}
"""

# javap prints a constructor under the fully-qualified class name, so a nested class
# shows up as Outer$Inner -- the one case where splitting on `$` is correct.
JAVAP_NESTED = """Compiled from "Outer.java"
class org.example.Outer$Inner {
  org.example.Outer$Inner(int);
    descriptor: (I)V
    Code:
       0: aload_0
       1: return

  static int access$100(org.example.Outer$Inner);
    descriptor: (Lorg/example/Outer$Inner;)I
    Code:
       0: iconst_0
       1: ireturn
}
"""

LAMBDA_DESC = "(Ljava/io/File;Ljava/lang/String;)Z"


def names(table):
    return {name for name, _desc in table}


# --------------------------------------------------------------------- lambdas

def test_lambda_methods_keep_their_full_name(parse):
    """`lambda$static$0` must not be reduced to `0`. The eval3 lookup key comes from
    PIT's own <mutatedMethod>, which is the full name, so a truncated key never matches
    and the row is written off as DISASM_ERR on both sides."""
    table = parse(JAVAP, "ClassPath")
    assert ("lambda$static$0", LAMBDA_DESC) in table
    assert ("lambda$static$1", LAMBDA_DESC) in table


def test_lambda_methods_are_not_keyed_under_a_bare_digit(parse):
    """The exact symptom of the bug: two distinct lambdas keyed as '0' and '1'."""
    assert not names(parse(JAVAP, "ClassPath")) & {"0", "1"}


def test_the_lambda_body_is_actually_captured(parse):
    body = parse(JAVAP, "ClassPath")[("lambda$static$0", LAMBDA_DESC)]
    assert body[0] == "aload_1"
    assert body[-1] == "ireturn"
    assert any("toLowerCase" in insn for insn in body)


def test_ecj_style_lambda_names_are_preserved_too(parse):
    """Eclipse names lambdas `lambda$0`; that also collapsed to `0`."""
    table = parse(JAVAP_ECJ, "ClassPath")
    assert ("lambda$0", LAMBDA_DESC) in table
    assert "0" not in names(table)


def test_synthetic_access_bridges_keep_their_name(parse):
    """`access$100` used to collapse to `100`."""
    table = parse(JAVAP_NESTED, "Inner")
    assert any(name == "access$100" for name, _ in table)
    assert "100" not in names(table)


# ---------------------------------------------------------------- constructors

def test_constructors_are_still_recognised(parse):
    """The `$` strip exists to turn javap's fully-qualified constructor name into
    `<init>`; fixing lambdas must not break that."""
    assert ("<init>", "(Ljava/lang/String;)V") in parse(JAVAP, "ClassPath")


def test_nested_class_constructors_are_still_recognised(parse):
    """`org.example.Outer$Inner(int)` -> `<init>`, the case the `$` split was for."""
    assert ("<init>", "(I)V") in parse(JAVAP_NESTED, "Inner")


# --------------------------------------------------------------- ordinary parse

def test_ordinary_methods_are_unaffected(parse):
    table = parse(JAVAP, "ClassPath")
    assert table[("getPath", "()Ljava/lang/String;")] == [
        "aload_0", "getfield Field classPath:Ljava/lang/String;", "areturn"
    ]


def test_every_method_in_the_class_is_indexed(parse):
    assert names(parse(JAVAP, "ClassPath")) == {
        "<init>", "getPath", "lambda$static$0", "lambda$static$1"
    }


def test_constant_pool_indexes_are_normalised_away(parse):
    """`#442` differs between our class and PIT's; the parser must erase it or every
    comparison would be spuriously DIVERGENT."""
    body = parse(JAVAP, "ClassPath")[("lambda$static$0", LAMBDA_DESC)]
    assert not any(re.search(r"#\d", insn) for insn in body)
