"""Invariants over the bytecode opcode tables and the public API surface.

These catch a whole class of typo/refactor regressions (a dropped opcode, a renamed
export) that would otherwise only surface as a silently unreconstructed mutation.
"""

import shared
from shared import (
    COND_BOUNDARY_OPCODES,
    COND_OPCODES,
    INVOKE_OPCODES,
    MATH_OPCODES,
    RETURN_OPCODES,
)


def test_math_opcodes_cover_the_five_arithmetic_families():
    assert set(MATH_OPCODES) == {
        "addition",
        "subtraction",
        "multiplication",
        "division",
        "modulus",
    }


def test_each_math_family_has_all_four_primitive_widths():
    """PIT mutates int/long/float/double arithmetic; a missing width means those
    mutations silently stop resolving."""
    for family, opcodes in MATH_OPCODES.items():
        prefixes = {op[0] for op in opcodes}
        assert prefixes == {"i", "l", "f", "d"}, f"{family} is missing a width: {opcodes}"


def test_math_opcode_families_are_disjoint():
    seen = set()
    for opcodes in MATH_OPCODES.values():
        assert not (seen & opcodes), "an opcode belongs to two arithmetic families"
        seen |= opcodes


def test_conditional_boundary_opcodes_are_a_subset_of_conditionals():
    assert COND_BOUNDARY_OPCODES <= COND_OPCODES


def test_boundary_opcodes_are_only_ordering_comparisons():
    """ConditionalsBoundary shifts < <= > >= ; equality/null checks have no boundary."""
    for op in COND_BOUNDARY_OPCODES:
        assert op.endswith(("lt", "le", "gt", "ge"))
    assert not any(op.endswith(("eq", "ne")) for op in COND_BOUNDARY_OPCODES)


def test_equality_and_null_conditionals_are_present_but_not_boundaries():
    for op in ("ifeq", "ifne", "ifnull", "ifnonnull", "if_acmpeq", "if_acmpne"):
        assert op in COND_OPCODES
        assert op not in COND_BOUNDARY_OPCODES


def test_return_opcodes_are_value_returns_only():
    """`return` (void) is not mutable to a value, so it must not be listed."""
    assert "return" not in RETURN_OPCODES
    assert RETURN_OPCODES == {"ireturn", "lreturn", "freturn", "dreturn", "areturn"}


def test_invoke_opcodes_cover_every_jvm_invoke_form():
    assert INVOKE_OPCODES == {
        "invokevirtual",
        "invokestatic",
        "invokeinterface",
        "invokespecial",
        "invokedynamic",
    }


def test_opcode_tables_are_non_empty_sets_of_strings():
    for table in (COND_OPCODES, COND_BOUNDARY_OPCODES, RETURN_OPCODES, INVOKE_OPCODES):
        assert table and all(isinstance(op, str) and op.islower() for op in table)


# --------------------------------------------------------------- public API


def test_declared_exports_all_exist():
    """Every name in __all__ must actually be importable -- a stale entry breaks
    `from shared import *` for both CLIs."""
    missing = [name for name in shared.__all__ if not hasattr(shared, name)]
    assert not missing, f"__all__ lists names that do not exist: {missing}"


def test_core_entry_points_are_exported():
    for name in (
        "load_source",
        "apply_mutation",
        "apply_mutation_with_fallback",
        "find_span_for_line",
        "extract_javadoc",
        "extract_test_files",
        "dataset_dirname",
    ):
        assert name in shared.__all__, f"{name} dropped from the public API"


def test_dataset_dirname_is_stable():
    """The notebook hardcodes this folder name; changing it silently breaks eval."""
    assert shared.dataset_dirname() == "PITMuS_dataset"
