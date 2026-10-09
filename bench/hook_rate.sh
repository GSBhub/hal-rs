#!/usr/bin/env bash
# Intercepts/second for a spinning HAL_UART_GetState: Rust ('return 0') vs
# Python (HALucinator ReturnZero in the embedded interpreter).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
ex="$here/../extern/halucinator/test/STM32/example"
secs=${SECS:-5}
halrs="$here/../python/.venv/bin/halrs"
cfg=(-c "$ex/Uart_Hyperterminal_IT_O0_config.yaml" -c "$ex/Uart_Hyperterminal_IT_O0_addrs.yaml"
     -c "$ex/Uart_Hyperterminal_IT_O0_memory.yaml" -c "$here/getstate_spin.yaml")
out=$(mktemp -d)
for mode in rust python; do
  extra=(); [[ $mode == rust ]] && extra=(--no-python)
  log="$out/$mode.log"
  timeout -s INT "$secs" "$halrs" run "${cfg[@]}" -n bench --outdir "$out/$mode" "${extra[@]}" >"$log" 2>&1 || true
  hits=$(grep -oP 'HAL_UART_GetState hit \K[0-9]+' "$log" || echo 0)
  echo "$mode: $hits hits in ${secs}s = $(( hits / secs )) /s ($(awk "BEGIN{ if ($hits>0) printf \"%.0f\", ${secs}e9/$hits; else print \"-\" }") ns/hit incl. guest loop)"
done
