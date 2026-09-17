#!/usr/bin/env bash
# Escáner de secretos sobre TODA la historia git (no solo el árbol actual).
# Uso: scan-secrets.sh <repo> [--tree-only]
set -uo pipefail
REPO="${1:?uso: scan-secrets.sh <repo> [--tree-only]}"
MODE="${2:-}"
REDACT="${SCAN_REDACT:-0}"
for a in "$@"; do [ "$a" = "--redact" ] && REDACT=1; done
export REDACT
cd "$REPO" || exit 1

PY=$(mktemp); trap 'rm -f "$PY"' EXIT
cat > "$PY" <<'PYEOF'
import os, re, sys, subprocess

REDACT = os.environ.get("REDACT") == "1"
PATTERNS = [
 ("clave privada",      rb"-----BEGIN (RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY"),
 ("token Telegram",     rb"\b\d{8,10}:[A-Za-z0-9_-]{30,40}\b"),
 ("OpenAI/Anthropic",   rb"\bsk-(ant-)?[A-Za-z0-9_-]{20,}"),
 ("Slack",              rb"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
 ("GitHub PAT",         rb"\bgh[pousr]_[A-Za-z0-9]{36,}"),
 ("AWS access key",     rb"\bAKIA[0-9A-Z]{16}\b"),
 ("Google API",         rb"\bAIza[0-9A-Za-z_-]{35}\b"),
 ("asignación secreta", rb"(?i)\b(pass(word|wd)?|secret|api[_-]?key|access[_-]?token|auth[_-]?token|bearer)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
 ("URL con credencial", rb"://[A-Za-z0-9._%-]+:[^/@\s'\"]{3,}@"),
]
# valores obviamente de ejemplo -> no son hallazgos
BENIGN = re.compile(rb"(?i)(your[_-]?|example|placeholder|changeme|xxx+|\.\.\.|<[a-z_]+>|dummy|fake|test|sample|tu[_-]?clave|aqui)")

def scan(data, label):
    # BENIGN se evalua SOLO sobre la linea del match: una ventana de +-40
    # caracteres cruza lineas y un "EXAMPLE" vecino silencia secretos reales.
    hits = []
    for name, pat in PATTERNS:
        for m in re.finditer(pat, data):
            ls = data.rfind(b"\n", 0, m.start()) + 1
            le = data.find(b"\n", m.end())
            if le == -1:
                le = len(data)
            linea = data[ls:le]
            if BENIGN.search(linea):
                continue
            n = data[:m.start()].count(b"\n") + 1
            hits.append((name, label, n, m.group()[:60].decode("utf-8", "replace")))
    return hits

total = 0
for line in sys.stdin:
    obj, path = line.rstrip("\n").split("\t", 1)
    try:
        data = subprocess.run(["git", "cat-file", "blob", obj],
                              capture_output=True, timeout=30).stdout
    except Exception:
        continue
    if b"\x00" in data[:8000]:      # binario
        continue
    for name, label, ln, frag in scan(data, path):
        total += 1
        # El fragmento es el secreto en claro: util para diagnosticar, peligroso
        # si la salida acaba en un fichero, un ticket o un pegado. --redact deja
        # tipo y ubicacion, que es lo que hace falta para ir a arreglarlo.
        shown = "<redactado>" if REDACT else repr(frag)
        print(f"  \u26a0 {name:<20} {label}:{ln}  {shown}")
print(f"\nhallazgos: {total}")
sys.exit(1 if total else 0)
PYEOF

if [ "$MODE" = "--tree-only" ] || [ "${3:-}" = "--tree-only" ]; then
  echo "== escaneando ÁRBOL ACTUAL de $REPO =="
  git ls-tree -r HEAD --format='%(objectname)%x09%(path)' | python3 "$PY"
else
  echo "== escaneando HISTORIA COMPLETA de $REPO =="
  n=$(git rev-list --all | wc -l); echo "   commits: $n"
  git rev-list --objects --all \
    | git cat-file --batch-check='%(objecttype) %(objectname) %(rest)' \
    | awk -F' ' '$1=="blob" {name=$2; $1=""; $2=""; sub(/^  /,""); if($0!="") print name"\t"$0}' \
    | sort -u | python3 "$PY"
fi
