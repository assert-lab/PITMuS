# PIT Mutation Testing — Source-Level Extraction

Extract source-level mutation data from PIT (Pitest) XML reports into a CSV with the original source line, a mutated line, the source file, the test file, and the line number.

## Overview

PIT operates at the bytecode level and does not export mutated source code. This tool bridges that gap by parsing PIT's XML output, mapping each mutation back to its source line, and applying the mutation description to produce a mutated source code line.

## Prerequisites

- Java 1.8+
- Maven
- Python 3.6+
- A Maven project with PIT configured (see POM configuration below)

## Step 1: Configure PIT for a Single Test File

In the `pom.xml`, scope PIT to a specific source class and test class. For example, to run only `StringUtilsTest` in Apache Commons Lang 3:

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
      <param>org.apache.commons.lang3.StringUtilsTest</param>
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

Key points:
- Set `targetTests` to avoid mutating the entire codebase.
- Set `outputFormats` to `XML` so the report can be parsed.

## Step 2: Run PIT

```bash
mvn clean test org.pitest:pitest-maven:mutationCoverage \
```

PIT writes its XML report to:

```
<project-name>/target/pit-reports/mutations.xml
```

## Step 3: Run the Parser

```bash
python parse_mutations.py <mutations.xml> <source_root> <output.csv>
```

Example:

```bash
python parse_mutations.py \
  commons-lang3/target/pit-reports/mutations.xml \
  commons-lang3/src/main/java \
  commons-lang3/mutations.csv
```

## Output

The CSV contains one row per mutation with the following columns:

| Column | Description |
|---|---|
| `mutation_line` | Original source code at the mutated line |
| `mutated_line` | Mutated source code |
| `source_file` | Path to the source file being mutated in the project source (e.g. `StringUtils.java`) |
| `line_number` | Line number in the source file |
| `description` | Mutation described in PIT xml file |

## Supported Mutators

The script handles all mutators in PIT's STRONGER group:

| Mutator | Example |
|---|---|
| ConditionalsBoundary | `>` to `>=` |
| Math | `+` to `-`, `*` to `/`, etc. |
| NegateConditionals | `==` to `!=` |
| RemoveConditionals | `if (x == y)` to `if (true)` |
| IncrementsMutator | `i++` to `i--` |
| InvertNegs | removes unary negation |
| VoidMethodCall | removes the method call entirely |
| BooleanTrueReturn / BooleanFalseReturn | `return x;` to `return true;` / `return false;` |
| NullReturn | `return x;` to `return null;` |
| EmptyObjectReturn | `return list;` to `return Collections.emptyList();` |
| PrimitiveReturns | `return x;` to `return 0;` |
| Bitwise / Shift | `&` to `|`, `<<` to `>>`, etc. |