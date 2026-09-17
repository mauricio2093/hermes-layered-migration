#!/usr/bin/env bash
# Corte del gateway: fósil -> clon limpio 0.21.3
set -uo pipefail
U="$HOME/.config/systemd/user/hermes-gateway.service"
C="$HOME/.hermes/hermes-oficial-clean"
R="$HOME/.hermes/rescue"
mkdir -p "$R"

echo "── 0. copia de seguridad de la unit ──"
cp "$U" "$R/unit-fosil-fichero.service" || { echo "✗ no se pudo copiar la unit; ABORTO"; exit 1; }
ls -l "$R/unit-fosil-fichero.service"

echo; echo "── 1. reescribir la unit ──"
# Regla global: hay 4 referencias al venv del fósil y una de ellas es
# ExecStopPost=-... , cuyo prefijo no encajaba en un patrón por línea.
# Primero se quita el node_modules del fósil, luego se repunta el venv entero.
sed -i \
 -e "s|$HOME/.hermes/hermes-agent/node_modules/.bin:||g" \
 -e "s|$HOME/.hermes/hermes-agent/venv|$C/.venv|g" \
 "$U"
grep -E "^(ExecStart|Environment|WorkingDirectory)=" "$U"

if grep -q "hermes-agent/venv" "$U"; then
  echo "✗ la unit sigue apuntando al fósil; restaurando y abortando"
  cp "$R/unit-fosil-fichero.service" "$U"; exit 1
fi

echo; echo "── 2. recargar y reiniciar ──"
systemctl --user daemon-reload
systemctl --user restart hermes-gateway.service
sleep 8

echo; echo "── 3. estado ──"
systemctl --user is-active hermes-gateway.service
ps -eo pid,etime,cmd | grep "hermes_cli.main gateway" | grep -v grep
echo; echo "── 4. últimos logs ──"
journalctl --user -u hermes-gateway.service -n 25 --no-pager 2>/dev/null | tail -25
