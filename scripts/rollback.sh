#!/usr/bin/env bash
# Rollback inmediato al fósil. No arreglar nada en caliente.
set -uo pipefail
U="$HOME/.config/systemd/user/hermes-gateway.service"
R="$HOME/.hermes/rescue"
L="$R/logs-fallo-$(date +%Y%m%d-%H%M%S).log"

echo "── conservando logs antes de revertir ──"
journalctl --user -u hermes-gateway.service -n 300 --no-pager > "$L" 2>&1
echo "  $L ($(wc -l < "$L") líneas)"

echo "── restaurando unit del fósil ──"
cp "$R/unit-fosil-fichero.service" "$U" || { echo "✗ sin copia de la unit"; exit 1; }
systemctl --user daemon-reload
systemctl --user restart hermes-gateway.service
sleep 8
systemctl --user is-active hermes-gateway.service
ps -eo pid,etime,cmd | grep "hermes_cli.main gateway" | grep -v grep
