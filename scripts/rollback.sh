#!/usr/bin/env bash
# Rollback del gateway: clon limpio (0.21.3) -> fósil (0.20.1).
#
#   soft (por defecto)  codigo antiguo + DB actual v30   <- probado funcionalmente
#   hard (--hard)       codigo antiguo + DB pre-corte v25
#
# HARD NUNCA es consecuencia automatica de que SOFT falle. Es una decision.
# Fail-closed: si falta cualquier artefacto, se aborta ANTES de tocar el sistema.
set -uo pipefail

HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
UNIT="${ROLLBACK_UNIT:-$HOME/.config/systemd/user/hermes-gateway.service}"
RESCUE="$HOME_DIR/rescue"
LIVE_DB="$HOME_DIR/state.db"
FOSSIL="$HOME_DIR/hermes-agent"
CLEAN="$HOME_DIR/hermes-oficial-clean"
UNIT_FOSSIL="$RESCUE/unit-fosil-fichero.service"
SERVICE="${ROLLBACK_SERVICE:-hermes-gateway.service}"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$RESCUE/rollback-$TS"
EXPECT_CURRENT=30      # schema esperado ahora
EXPECT_TARGET=25       # schema del fosil

MODE=soft; DRY=0; FORCE=0
for a in "$@"; do case "$a" in
  --hard) MODE=hard ;;
  --soft) MODE=soft ;;
  --dry-run) DRY=1 ;;
  --force) FORCE=1 ;;
  -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
  *) echo "opcion desconocida: $a"; exit 2 ;;
esac; done

say()  { printf '%s\n' "$*"; }
die()  { printf '\n✗ ABORTADO: %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else "$@"; fi; }

sqlite_meta() {  # $1=ruta -> "version|integrity|bytes"
  python3 - "$1" <<'PY' 2>/dev/null
import sqlite3, sys, os
p = sys.argv[1]
try:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    v = c.execute("select version from schema_version").fetchone()[0]
    i = c.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"{v}|{i}|{os.path.getsize(p)}")
except Exception as e:
    print(f"?|{e}|0")
PY
}

snapshot_db() {  # $1=origen $2=destino — backup API, NUNCA cp sobre una DB viva
  python3 - "$1" "$2" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
}

service_stopped() {  # true solo si systemd lo da por inactivo Y no queda PID
  local st pid
  st="$(systemctl --user is-active "$SERVICE" 2>/dev/null)"
  pid="$(systemctl --user show "$SERVICE" -p MainPID --value 2>/dev/null)"
  [ "$st" != "active" ] && [ "$st" != "activating" ] && { [ -z "$pid" ] || [ "$pid" = "0" ]; }
}

say "════════════════════════════════════════════"
say "Rollback mode: $(echo "$MODE" | tr a-z A-Z)$([ "$DRY" = 1 ] && echo '  (DRY-RUN)')"
say "════════════════════════════════════════════"

# ── 1. validaciones fail-closed ────────────────────────────────────────
say; say "── 1. artefactos requeridos ──"
[ -f "$UNIT" ]            || die "no existe la unit en uso: $UNIT"
[ -f "$UNIT_FOSSIL" ]     || die "no existe la unit del fosil: $UNIT_FOSSIL"
[ -d "$FOSSIL/.git" ]     || die "no existe el checkout del fosil: $FOSSIL"
[ -x "$FOSSIL/venv/bin/python" ] || die "el venv del fosil no es ejecutable"
[ -f "$LIVE_DB" ]         || die "no existe la DB actual: $LIVE_DB"
grep -q "hermes-agent/venv" "$UNIT_FOSSIL" || die "la unit del fosil no apunta al fosil"
say "   ✓ unit en uso, unit del fosil, checkout, venv y DB presentes"

cur="$(sqlite_meta "$LIVE_DB")"; cur_v="${cur%%|*}"; cur_rest="${cur#*|}"; cur_i="${cur_rest%%|*}"
[ "$cur_i" = "ok" ] || die "integrity_check de la DB actual: $cur_i"
if [ "$cur_v" != "$EXPECT_CURRENT" ] && [ "$FORCE" != 1 ]; then
  die "DB actual en schema $cur_v, se esperaba $EXPECT_CURRENT (usa --force si es intencionado)"
fi
say "   ✓ DB actual: schema $cur_v, integrity ok"

BACKUP_DB=""
if [ "$MODE" = hard ]; then
  # el backup compatible MAS RECIENTE, no uno fijo: menos estado perdido
  for d in $(ls -d "$HOME_DIR"/backups/independiente/*/ 2>/dev/null | sort -r); do
    cand="$d/db-consistent/state.db"; [ -f "$cand" ] || continue
    m="$(sqlite_meta "$cand")"
    if [ "${m%%|*}" = "$EXPECT_TARGET" ] && [ "$(echo "$m" | cut -d'|' -f2)" = "ok" ]; then
      BACKUP_DB="$cand"; break
    fi
  done
  [ -n "$BACKUP_DB" ] || die "sin backup verificado con schema $EXPECT_TARGET"
  say "   ✓ backup destino: $(basename "$(dirname "$(dirname "$BACKUP_DB")")") (schema $EXPECT_TARGET)"
fi

# ── 2. snapshot de seguridad ANTES de tocar nada ───────────────────────
say; say "── 2. snapshot consistente de la DB actual ──"
run mkdir -p "$OUT"
SNAP="$OUT/state-pre-rollback-$TS.db"
if [ "$DRY" = 1 ]; then
  say "   [dry-run] snapshot -> $SNAP"
else
  snapshot_db "$LIVE_DB" "$SNAP" || die "no se pudo crear el snapshot"
  s="$(sqlite_meta "$SNAP")"; s_v="${s%%|*}"; s_i="$(echo "$s" | cut -d'|' -f2)"; s_b="$(echo "$s" | cut -d'|' -f3)"
  [ "$s_i" = "ok" ]      || die "snapshot corrupto: integrity=$s_i"
  [ "$s_b" -gt 0 ]       || die "snapshot de tamano cero"
  [ "$s_v" = "$cur_v" ]  || die "snapshot en schema $s_v, la DB actual esta en $cur_v"
  say "   ✓ $SNAP"
  say "     schema $s_v · integrity ok · $((s_b/1048576)) MB"
fi

# ── 3. material para deshacer ESTE rollback ────────────────────────────
say; say "── 3. material de recuperacion (volver a 0.21.3) ──"
if [ "$DRY" = 1 ]; then
  say "   [dry-run] cp $UNIT -> $OUT/unit-actual.service"
  say "   [dry-run] cp $HOME_DIR/config.yaml -> $OUT/config-actual.yaml"
else
  cp "$UNIT" "$OUT/unit-actual.service" || die "sin material de recuperacion: no se pudo copiar la unit"
  cp "$HOME_DIR/config.yaml" "$OUT/config-actual.yaml" || die "sin material de recuperacion: no se pudo copiar config.yaml"
fi
if [ "$DRY" = 0 ]; then
  { echo "rollback_ts=$TS"; echo "mode=$MODE"; echo "db_schema_before=$cur_v";
    echo "unit_before=$OUT/unit-actual.service"; echo "config_before=$OUT/config-actual.yaml";
    echo "snapshot=$SNAP"; echo "clean_checkout=$CLEAN"; } > "$OUT/recovery.env"
fi
say "   ✓ unit, config y snapshot guardados en $OUT"
say "     volver a 0.21.3: cp \$unit_before \"$UNIT\" && systemctl --user daemon-reload && systemctl --user restart $SERVICE"

# ── 4. parar escritores (obligatorio si se sustituye la DB) ────────────
say; say "── 4. detener el servicio ──"
if [ "$DRY" = 1 ]; then
  say "   [dry-run] systemctl --user stop $SERVICE"
else
  systemctl --user stop "$SERVICE" || die "no se pudo detener $SERVICE; la DB NO se ha tocado"
  # Un stop que devuelve 0 no garantiza que el proceso haya muerto: se espera y
  # se comprueba. Sustituir la DB con un escritor vivo la corrompe.
  for _ in 1 2 3 4 5 6 7 8 9 10; do service_stopped && break; sleep 1; done
  service_stopped || die "$SERVICE sigue vivo tras 10s; la DB NO se ha tocado"
  say "   ✓ $SERVICE detenido y verificado"
fi

# ── 5. restaurar codigo / unit ─────────────────────────────────────────
say; say "── 5. restaurar unit del fosil ──"
if [ "$DRY" = 1 ]; then
  say "   [dry-run] cp -p $UNIT_FOSSIL -> $UNIT"
  say "   [dry-run] systemctl --user daemon-reload"
else
  cp -p "$UNIT_FOSSIL" "$UNIT" || die "no se pudo restaurar la unit del fosil"
  systemctl --user daemon-reload || die "daemon-reload fallo; la unit quedo escrita pero no cargada"
fi
say "   ✓ unit restaurada (codigo 0.21.3 -> 0.20.1)"

# ── 6. base de datos ───────────────────────────────────────────────────
say; say "── 6. base de datos ──"
if [ "$MODE" = soft ]; then
  say "   KEEP: se conserva la DB actual (schema $cur_v)"
else
  say "   REPLACE: schema $cur_v -> $EXPECT_TARGET"
  if [ "$DRY" = 1 ]; then
    say "   [dry-run] restaurar $BACKUP_DB -> $LIVE_DB (preservando permisos)"
  else
    PERMS="$(stat -c %a "$LIVE_DB")"; OWNER="$(stat -c %U:%G "$LIVE_DB")"
    # la actual NO se borra: ya existe el snapshot validado
    mv "$LIVE_DB" "$OUT/state-desplazada-$TS.db" || die "no se pudo desplazar la DB actual"
    rm -f "$LIVE_DB-wal" "$LIVE_DB-shm"
    snapshot_db "$BACKUP_DB" "$LIVE_DB" || die "no se pudo restaurar la DB destino"
    chmod "$PERMS" "$LIVE_DB" || die "no se pudieron restaurar los permisos de la DB"
    chown "$OWNER" "$LIVE_DB" 2>/dev/null || true   # sin privilegios es normal; el modo ya esta puesto
    r="$(sqlite_meta "$LIVE_DB")"; r_v="${r%%|*}"; r_i="$(echo "$r" | cut -d'|' -f2)"
    [ "$r_i" = "ok" ]           || die "DB restaurada corrupta: $r_i"
    [ "$r_v" = "$EXPECT_TARGET" ] || die "DB restaurada en schema $r_v, se esperaba $EXPECT_TARGET"
    say "   ✓ restaurada: schema $r_v · integrity ok · permisos $PERMS"
  fi
fi

# ── 7. arrancar y comprobar ────────────────────────────────────────────
say; say "── 7. arrancar y health check ──"
if [ "$DRY" = 1 ]; then
  say "   [dry-run] systemctl --user start $SERVICE"
else
  systemctl --user start "$SERVICE" || say "   ✗ start fallo; se conserva todo en $OUT"
  sleep 10
fi
if [ "$DRY" = 1 ]; then
  say "   [dry-run] health check"
  ACTIVE="(dry-run)"; HEALTH="(dry-run)"
else
  ACTIVE="$(systemctl --user is-active "$SERVICE" 2>/dev/null)"
  NR="$(systemctl --user show "$SERVICE" -p NRestarts --value 2>/dev/null)"
  PID="$(systemctl --user show "$SERVICE" -p MainPID --value 2>/dev/null)"
  sleep 5
  PID2="$(systemctl --user show "$SERVICE" -p MainPID --value 2>/dev/null)"
  if [ "$ACTIVE" = "active" ] && [ "$PID" = "$PID2" ] && [ -n "$PID" ] && [ "$PID" != "0" ]; then
    HEALTH="passed"
  else
    HEALTH="FAILED (active=$ACTIVE pid=$PID->$PID2 restarts=$NR)"
  fi
fi

say; say "════════════════════════════════════════════"
say "Rollback mode: $(echo "$MODE" | tr a-z A-Z)"
say "Code: 0.21.3 -> 0.20.1"
[ "$MODE" = soft ] && say "Database: KEEP current schema $cur_v" \
                   || say "Database: schema $cur_v -> $EXPECT_TARGET"
say "Current DB preserved at: $SNAP"
say "Recovery material: $OUT"
say "Service: $ACTIVE"
say "Health check: $HEALTH"
say "════════════════════════════════════════════"

if [ "$DRY" = 0 ] && [ "$HEALTH" != "passed" ]; then
  say
  say "El arranque FALLO. No se ejecuta hard rollback automaticamente y no se"
  say "destruye estado. Artefactos conservados en $OUT."
  say "Revisa: journalctl --user -u $SERVICE -n 100 --no-pager"
  exit 1
fi
exit 0
