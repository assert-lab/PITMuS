# mutate-source-code

Extract and inject source-level mutations from PIT (Pitest) XML reports.

PIT operates at the bytecode level and does not export mutated source code. This tool bridges that gap by parsing PIT's XML output, mapping each mutation back to its source line, and applying the mutation description to produce a mutated source code line.

## Repository Structure

```
mutate-source-code/
├── extract_mutated_src_code.py         ← created CSV files with mutated source lines
├── inject_mutated_src_code.py          ← injects mutated source lines into source file
└── test-projects/
    └── <project-name>/
        ├── src/main/java/              ← source codes
        ├── target/pit-reports/mutations.xml
        └── mutated_src_lines/          ← generated output
            ├── ClassName1.csv
            ├── ClassName2.csv
            └── ...
```

## Prerequisites

- Python 3.6+
- `javalang` — `pip install javalang`
- A Maven project with PIT configured and a generated `mutations.xml` report

## Usage

### Step 1: Extract Mutated Source Lines

```bash
python extract_mutated_src_code.py <project-name>
```

Example:

```bash
python extract_mutated_src_code.py joda-time
```

This reads `test-projects/joda-time/target/pit-reports/mutations.xml`, resolves each mutation to its source line, applies the mutation, and writes one CSV per source file into `test-projects/joda-time/mutated_src_lines/`.

#### Output Format

Each CSV contains one row per mutation:

| Column | Description |
|---|---|
| `mutation_line` | Original source code at the mutated line |
| `mutated_line` | Source code after applying the mutation |
| `source_file` | Path to the source file (e.g. `org/joda/time/DateTime.java`) |
| `line_number` | Line number in the source file |
| `description` | PIT's mutation description |
| `test_file` | Test file(s) covering the mutation, separated by `\|` |

### Step 2: Inject Mutations into Source

```bash
python inject_mutated_src_code.py <project-name>
```

This reads the CSVs from `mutated_src_lines/` and injects the mutated lines into copies of the source files.

## Supported Mutators

The extraction script handles all mutators in PIT's STRONGER group using `javalang` tokenization for precise operator-level replacements:

| Mutator | Example |
|---|---|
| ConditionalsBoundary | `>` → `>=` |
| Math | `+` → `-`, `*` → `/`, `%` → `*`, etc. |
| NegateConditionals | `==` → `!=`, `>=` → `<` |
| RemoveConditionals | `if (x == y)` → `if (true)`, ternary conditions |
| IncrementsMutator | `i++` → `i--`, `-4` → `4` |
| InvertNegs | removes unary negation |
| VoidMethodCall | removes the method call entirely |
| Return values | `return x;` → `return null;` / `return true;` / `return Collections.emptyMap();` / etc. |
| Bitwise / Shift | `&` → `\|`, `<<` → `>>`, etc. |

## Generating a PIT Report

If you need to generate a PIT mutation report for a Maven project, add the following plugin to the project's `pom.xml`:

```xml
<plugin>
  <groupId>org.pitest</groupId>
  <artifactId>pitest-maven</artifactId>
  <version>1.22.0</version>
  <dependencies>
    <dependency>
      <groupId>org.pitest</groupId>
      <artifactId>pitest-junit5-plugin</artifactId>
      <version>1.2.1</version>
    </dependency>
  </dependencies>
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
    <threads>4</threads>
    <timeoutConstant>16000</timeoutConstant>
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