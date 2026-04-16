# PITMuS: PIT Mutations In the Source Code

This repository is for two things: extract and inject source-level mutations from PIT (Pitest) XML reports.

PIT operates at the bytecode level and does not export mutated source code. This tool bridges that gap by parsing PIT's XML output, mapping each mutation back to its source line, and applying the mutation description to produce a mutated source code line.

## Repository Structure

```
mutate-source-code/
├── scripts/
│   ├── extract.py                      ← creates CSV files with mutated source lines
│   └── inject.py                       ← injects mutated source lines into source files
├── PITMuS_dataset/
│   └── <project-name>/
│       ├── mutated_methods.csv         ← full method bodies (original + mutated) with Javadoc
│       └── meta.csv                    ← corresponding metadata (line_no, original_line, mutated_line, source_filepath)
└── test-projects/
    └── <project-name>/
        ├── src/main/java/              ← source code
        ├── target/pit-reports/mutations.xml
        ├── mutated_src_lines/          ← generated CSVs (one per source file)
        │   ├── ClassName1.csv
        │   ├── ClassName2.csv
        │   └── ...
        └── injected_mutants/           ← generated mutant source files
            ├── ClassName1_line68_mutant1.java
            ├── ClassName1_line68_mutant2.java
            └── ...
```

## Prerequisites

- Python 3.6+
- javalang
- A JDK on `PATH` (the extractor invokes `javap` to read compiled `.class` files for bytecode-accurate mutation targeting)
- A Maven project with PIT configured, a generated `mutations.xml` report, and compiled classes under `target/classes/`

## Usage

### Step 1: Extract Mutated Source Lines

Run from the repository root:

```bash
python scripts/extract.py <project-name> <mode>
```

The script supports two modes:

- **`dataset`** — creates project-level CSVs in `PITMuS_dataset/<project-name>/` (`mutated_methods.csv` and `meta.csv`)
- **`file-wise`** — creates per-source-file CSVs in `test-projects/<project-name>/mutated_src_lines/`

Examples:

```bash
python scripts/extract.py joda-time dataset
python scripts/extract.py joda-time file-wise
```

This reads `test-projects/joda-time/target/pit-reports/mutations.xml`, resolves each mutation to its source line (using PIT's `<indexes><index>` bytecode offsets + `javap` output from `target/classes/` to target the exact token), applies the mutation, and writes the output according to the selected mode.

#### Output Format

**Per-source-file CSV** (`mutated_src_lines/<ClassName>.csv`) — one row per mutation:

| Column | Description |
|---|---|
| `mutation_line` | Original source code at the mutated line |
| `mutated_line` | Source code after applying the mutation |
| `source_file` | Path to the source file (e.g. `org/joda/time/DateTime.java`) |
| `line_number` | Line number in the source file |
| `description` | PIT's mutation description |
| `test_file` | Test file(s) covering the mutation, separated by `\|` |

**`PITMuS_dataset/<project-name>/mutated_methods.csv`** — one row per mutation, full method bodies:

| Column | Description |
|---|---|
| `id` | row identifier, shared with `meta.csv` (see below) |
| `original_method` | Full body of the method containing the mutated line |
| `mutated_method` | Same method body with the mutated line substituted |
| `docstring` | Javadoc block (`/** ... */`) immediately preceding the method, or empty |

**`PITMuS_dataset/<project-name>/meta.csv`** — row-aligned with `mutated_methods.csv` via the shared `id` column:

| Column | Description |
|---|---|
| `id` | Same id as the corresponding row in `mutated_methods.csv` |
| `line_no` | Line number in the source file |
| `original_line` | Original source line |
| `mutated_line` | Mutated source line |
| `source_filepath` | Path to the source file |

### Step 2: Inject Mutations into Source

The injection script supports three modes depending on how many arguments are provided:

```bash
# Inject all mutations from all files
python scripts/inject.py <project-name>

# Inject all mutations for a specific source file
python scripts/inject.py <project-name> <source-file>

# Inject mutations for a specific source file and line number
python scripts/inject.py <project-name> <source-file> <line-number>
```

Examples:

```bash
python scripts/inject.py joda-time
python scripts/inject.py joda-time PeriodFormatterBuilder.java
python scripts/inject.py joda-time PeriodFormatterBuilder.java 1377
```

Each mutant is a full copy of the original source file with one line replaced. Output is written to `test-projects/<project-name>/injected_mutants/`.

## Supported Mutators

The extraction script handles all 13 mutators in PIT's STRONGER group (DEFAULTS + `REMOVE_CONDITIONALS` + `EXPERIMENTAL_SWITCH`). 

| Mutator | Example |
|---|---|
| ConditionalsBoundary | `>` → `>=`, etc. |
| Math | `+` → `-`, `*` → `/`, `%` → `*`, etc. |
| NegateConditionals | `==` → `!=`, `>=` → `<` |
| RemoveConditionals | `if (x == y)` → `if (true)`, ternary conditions |
| IncrementsMutator | `i++` → `i--`, `-4` → `4`, etc. |
| InvertNegs | removes unary negation |
| VoidMethodCall | removes the method call entirely |
| Empty / Null / Primitive / True / False Returns | `return x;` → `return null;` / `return true;` / `return Collections.emptyMap();` / etc. |
| Bitwise / Shift | `&` → `\|`, `<<` → `>>`, etc. |


## Generating a PIT Report

If you need to generate a PIT mutation report for a Maven project, add the following plugin to the project's `pom.xml`. The example below is configured for Apache Commons Lang 3 — update `targetClasses` and `targetTests` to match the subject project's package structure.
 
```xml
<plugin>
  <groupId>org.pitest</groupId>
  <artifactId>pitest-maven</artifactId>
  <version>1.22.0</version>
  <configuration>
    <targetClasses>
      <param>org.apache.commons.lang3.*</param>
    </targetClasses>
    <targetTests>
      <param>org.apache.commons.lang3.*</param>
    </targetTests>
    <mutators>
      <mutator>STRONGER</mutator>
    </mutators>
    <fullMutationMatrix>true</fullMutationMatrix>
    <exportLineCoverage>true</exportLineCoverage>
    <outputFormats>XML</outputFormats>
  </configuration>
</plugin>
```

Then run:

```bash
mvn clean test org.pitest:pitest-maven:mutationCoverage
```

The XML report is written to `target/pit-reports/mutations.xml`.

## Demo

[![Demo video](https://img.youtube.com/vi/37TtM6UfYMQ/hqdefault.jpg)](https://youtu.be/37TtM6UfYMQ)