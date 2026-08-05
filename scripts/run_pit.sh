#!/bin/bash
set -u

TEST_PROJECTS_DIR="/Users/nulfat/Documents/Projects/PhD/PITMuS/PITMuS/test-projects"

projects=(
    bcel
    commons-beanutils
    commons-dbutils
    commons-jexl3
    http-request
    joda-time
    jsoup
)

failed=()
for project in "${projects[@]}"; do
    project_dir="$TEST_PROJECTS_DIR/$project"
    echo "============================================================"
    echo "Running pit.sh in $project"
    echo "============================================================"
    if [[ ! -f "$project_dir/pit.sh" ]]; then
        echo "SKIP: $project_dir/pit.sh not found"
        failed+=("$project (missing)")
        continue
    fi
    (cd "$project_dir" && bash pit.sh)
    status=$?
    if [[ $status -ne 0 ]]; then
        echo "FAILED: $project (exit $status)"
        failed+=("$project (exit $status)")
    fi
done

echo "============================================================"
if [[ ${#failed[@]} -eq 0 ]]; then
    echo "All projects completed successfully."
else
    echo "Failures:"
    for f in "${failed[@]}"; do
        echo "  - $f"
    done
    exit 1
fi
