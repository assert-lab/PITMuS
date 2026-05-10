#!/bin/bash
# Run sampled-mutant evaluation for the 4 new projects sequentially.
# Resume-safe: each project's run_evaluation.py is invoked with --resume.
set -u

ROOT="/home/tasfia/Desktop/EvoAI/demo_pitmus/PITMuS"
PROJECTS=(commons-dbutils commons-beanutils commons-jexl3 jsoup)
RESULTS="$ROOT/evaluation/results"
TIMING="$RESULTS/overall_timing.txt"
LOG="$RESULTS/run_all.log"

mkdir -p "$RESULTS"
echo "[run_all] started at $(date -Is)" | tee -a "$LOG"
echo "projects: ${PROJECTS[*]}" | tee -a "$LOG"

T0=$(date +%s)
for project in "${PROJECTS[@]}"; do
    P_T0=$(date +%s)
    P_START_ISO=$(date -Is)
    echo | tee -a "$LOG"
    echo "===== starting $project at $P_START_ISO =====" | tee -a "$LOG"

    python3 -u "$ROOT/evaluation/scripts/run_evaluation.py" "$project" --resume --timeout 90 2>&1 | tee -a "$LOG"
    rc=${PIPESTATUS[0]}

    P_T1=$(date +%s)
    P_DUR=$((P_T1 - P_T0))
    P_END_ISO=$(date -Is)
    echo "===== $project finished at $P_END_ISO (exit=$rc, $P_DUR s) =====" | tee -a "$LOG"
    if [[ $P_DUR -gt 14400 ]]; then
        echo "[warning] $project took $P_DUR s (> 4h)" | tee -a "$LOG"
    fi
done

T1=$(date +%s)
TOTAL=$((T1 - T0))
echo | tee -a "$LOG"
echo "[run_all] total wall clock: ${TOTAL}s" | tee -a "$LOG"
echo "[run_all] generating combined report..." | tee -a "$LOG"
python3 -u "$ROOT/evaluation/scripts/combine_results.py" "${PROJECTS[@]}" 2>&1 | tee -a "$LOG"
echo "[run_all] done at $(date -Is)" | tee -a "$LOG"
