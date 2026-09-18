#!/usr/bin/env bash
# Backup A — independiente del actualizador de Hermes.
#
# Regla: "verificado" = restaurado y validado por un mecanismo independiente,
# nunca leído por la misma herramienta que lo creó.
#
# Las DB SQLite NO se copian en caliente con tar: se excluyen del archivo y se
# capturan con Connection.backup() en db-consistent/, que es la fuente
# autoritativa en un restore.
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
BASE="$(basename "$HERMES_HOME")"
TS="$(date +%Y%m%d-%H%M%S)"
DEST="$HERMES_HOME/backups/independiente/$TS"
DBDIR="$DEST/db-consistent"
ARCHIVE="$DEST/hermes-home-$TS.tar.gz"
STATE="$DEST/state.json"

# Conteos de FICHEROS FUENTE (sin __pycache__/*.pyc, que el tar excluye).
# Revisados a mano 2026-09-18. Si cambian legítimamente, actualizar aquí
# conscientemente: son la defensa contra un glob que respalde media instalación.
# anchors=27: sus 28 fuentes menos anchors/data/phone.db, que viaja en db-consistent/.
#
# scripts 5 -> 8 (2026-09-18, BACKUP-DECL-006). Los tres nuevos se verificaron
# uno a uno contra el tar del ultimo backup verificado (20260916-182215), que
# contenia exactamente los cinco anteriores:
#   cutover.sh       corte del gateway fosil -> clon limpio
#   push-limpio.sh   publicacion de la historia limpia
#   rollback.sh      la herramienta de recuperacion; ninguna razon la justifica
#                    mas que esta para estar dentro del backup
# Los tres son bash escrito a mano, 0700, sin secretos. Ninguno es temporal,
# generado, cache ni artefacto de pruebas.
declare -A EXPECT=( [plugins]=6 [scripts]=8 [patches]=6 [anchors]=27 [voice-samples]=7 )
# 6 -> 8 (2026-09-18, BACKUP-DECL-006). Hermes 0.21.3 trajo dos bases nuevas que
# no capturaba nadie: el tar excluye *.db y no estaban declaradas aqui, asi que
# habrian faltado en silencio. Entran por la misma via que las demas -- copia
# consistente con Connection.backup() en db-consistent/ e integrity_check --
# sin excepciones:
#   shared-state.db       Hosted Rooms / coordinacion durable
#   cron/deliveries.db    cola durable de entregas cron + tombstones
DBS=(state.db verification_evidence.db kanban.db shared-state.db cron/executions.db cron/deliveries.db cron/notepad.db anchors/data/phone.db)
SECRETS=(.env auth.json config.yaml channel_directory.json)

ok_created=false; ok_archive=false; ok_db=false; ok_manifest=false; ok_restore=false
step() { echo; echo "── $* ──"; }
flat() { echo "$1" | tr '/' '_'; }

mkdir -p "$DBDIR" || exit 1; chmod 700 "$DEST"

step "1. SQLite .backup (copia consistente en caliente)"
db_ok=true
for db in "${DBS[@]}"; do
  src="$HERMES_HOME/$db"
  [ -f "$src" ] || { echo "   ✗ ausente: $db"; db_ok=false; continue; }
  out="$DBDIR/$(flat "$db")"
  python3 - "$src" "$out" <<'PY' || { db_ok=false; continue; }
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
  printf '   %-28s -> %s\n' "$db" "$(du -h "$out" | cut -f1)"
done

step "2. integrity_check sobre las COPIAS"
for db in "${DBS[@]}"; do
  out="$DBDIR/$(flat "$db")"; [ -f "$out" ] || continue
  r=$(python3 -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" "$out" 2>&1)
  printf '   %-28s %s\n' "$db" "$r"; [ "$r" = "ok" ] || db_ok=false
done
# Invariante: el número de bases capturadas debe igualar al esperado.
# Si mañana aparece una séptima, este script debe FALLAR RUIDOSAMENTE en vez
# de dejarla fuera en silencio. anchors/data/phone.db apareció así.
expected_databases=${#DBS[@]}
consistent_backups=$(find "$DBDIR" -type f -name '*.db' | wc -l)
echo "   expected_databases=$expected_databases  consistent_backups=$consistent_backups"
if [ "$consistent_backups" -ne "$expected_databases" ]; then
  echo "   ✗ ABORT: faltan copias consistentes"; db_ok=false
fi
# detectar bases NO declaradas en DBS
undeclared=$(find "$HERMES_HOME" -name '*.db' \
  -not -path "$HERMES_HOME/backups/*" -not -path "$HERMES_HOME/rescue/*" \
  -not -path "$HERMES_HOME/hermes-agent/*" -not -path "$HERMES_HOME/hermes-oficial-clean/*" \
  -not -path "$HERMES_HOME/cache/*" 2>/dev/null | wc -l)
if [ "$undeclared" -ne "$expected_databases" ]; then
  echo "   ✗ ABORT: $undeclared bases en disco vs $expected_databases declaradas"
  find "$HERMES_HOME" -name '*.db' -not -path "$HERMES_HOME/backups/*" \
    -not -path "$HERMES_HOME/rescue/*" -not -path "$HERMES_HOME/hermes-agent/*" \
    -not -path "$HERMES_HOME/hermes-oficial-clean/*" -not -path "$HERMES_HOME/cache/*" 2>/dev/null \
    | sed "s|$HERMES_HOME/|      |"
  db_ok=false
fi
$db_ok && ok_db=true

step "3+4. Empaquetar (excluye regenerables y las DB en caliente)"
tar -czf "$ARCHIVE" -C "$(dirname "$HERMES_HOME")" \
  --exclude="$BASE/backups" --exclude="$BASE/rescue" \
  --exclude="$BASE/hermes-oficial-clean" \
  --exclude="$BASE/hermes-agent/venv" --exclude="$BASE/hermes-agent/.git" \
  --exclude="$BASE/hermes-agent/node_modules" \
  --exclude="$BASE/node" --exclude="$BASE/lsp" \
  --exclude="$BASE/cache" --exclude="$BASE/audio_cache" \
  --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  "$BASE" 2>"$DEST/tar-warnings.log"
[ -s "$ARCHIVE" ] && ok_created=true
echo "   archivo: $(du -h "$ARCHIVE" | cut -f1)   avisos: $(wc -l < "$DEST/tar-warnings.log")"

step "5. Permisos esperados de secretos"
for f in "${SECRETS[@]}"; do
  [ -f "$HERMES_HOME/$f" ] && printf '%s %s\n' "$(stat -c %a "$HERMES_HOME/$f")" "$f"
done | tee "$DEST/perms-esperados.txt"

step "6. SHA-256 del conjunto"
( cd "$DEST" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256 )
( cd "$DEST" && sha256sum -c MANIFEST.sha256 --quiet ) && ok_manifest=true
echo "   $(wc -l < "$DEST/MANIFEST.sha256") ficheros"

step "7. Integridad del archivo"
gzip -t "$ARCHIVE" && tar -tzf "$ARCHIVE" >/dev/null 2>&1 && ok_archive=true
echo "   gzip+tar: $($ok_archive && echo OK || echo FALLO)"

step "8+9. Restore temporal y validación de CONTENIDO"
RT=$(mktemp -d); trap 'rm -rf "$RT"' EXIT
tar -xzf "$ARCHIVE" -C "$RT" 2>/dev/null
H="$RT/$BASE"; restore_ok=true

# el archivo NO debe traer DB en caliente
stray=$(find "$H" -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' 2>/dev/null | wc -l)
[ "$stray" -eq 0 ] && echo "   ✓ archivo sin DB en caliente" \
                   || { echo "   ✗ $stray DB en caliente coladas"; restore_ok=false; }

while read -r perm f; do
  if [ -f "$H/$f" ]; then
    p=$(stat -c %a "$H/$f")
    [ "$p" = "$perm" ] && printf '   ✓ %-26s perms %s\n' "$f" "$p" \
                       || { printf '   ✗ %-26s perms %s != %s\n' "$f" "$p" "$perm"; restore_ok=false; }
  else printf '   ✗ %-26s AUSENTE\n' "$f"; restore_ok=false; fi
done < "$DEST/perms-esperados.txt"

for d in "${!EXPECT[@]}"; do
  n=$(find "$H/$d" -type f ! -path '*__pycache__*' ! -name '*.pyc' 2>/dev/null | wc -l); e=${EXPECT[$d]}
  [ "$n" -eq "$e" ] && printf '   ✓ %-26s %s/%s\n' "$d/" "$n" "$e" \
                    || { printf '   ✗ %-26s %s/%s\n' "$d/" "$n" "$e"; restore_ok=false; }
done

# las DB se validan desde db-consistent/, la fuente autoritativa
for db in "${DBS[@]}"; do
  out="$DBDIR/$(flat "$db")"
  if [ -f "$out" ]; then
    r=$(python3 -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" "$out" 2>&1)
    n=$(python3 -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute(\"select count(*) from sqlite_master\").fetchone()[0])" "$out" 2>&1)
    [ "$r" = "ok" ] && printf '   ✓ %-26s integrity=ok objetos=%s\n' "$db" "$n" \
                    || { printf '   ✗ %-26s integrity=%s\n' "$db" "$r"; restore_ok=false; }
  else printf '   ✗ %-26s copia AUSENTE\n' "$db"; restore_ok=false; fi
done
$restore_ok && ok_restore=true

step "10. Estado (derivado, nunca fijado a mano)"
cat > "$DEST/RESTORE.md" <<EOF
# Restaurar este backup

    tar -xzf $(basename "$ARCHIVE") -C /destino
    # El tar trae las entradas de directorio, pero esto no cuesta nada y cubre
    # un destino donde cron/ o anchors/data/ no se hayan materializado:
    mkdir -p /destino/$BASE/cron /destino/$BASE/anchors/data
    # las DB NO están en el tar; copiarlas desde db-consistent/ (8):
    cp db-consistent/state.db                  /destino/$BASE/state.db
    cp db-consistent/verification_evidence.db  /destino/$BASE/verification_evidence.db
    cp db-consistent/kanban.db                 /destino/$BASE/kanban.db
    cp db-consistent/shared-state.db           /destino/$BASE/shared-state.db
    cp db-consistent/cron_executions.db        /destino/$BASE/cron/executions.db
    cp db-consistent/cron_deliveries.db        /destino/$BASE/cron/deliveries.db
    cp db-consistent/cron_notepad.db           /destino/$BASE/cron/notepad.db
    cp db-consistent/anchors_data_phone.db     /destino/$BASE/anchors/data/phone.db

Verificar antes: sha256sum -c MANIFEST.sha256
EOF
python3 - "$STATE" "$ok_created" "$ok_archive" "$ok_db" "$ok_manifest" "$ok_restore" "$ARCHIVE" <<'PY'
import json, sys, os, datetime
p, *f, arch = sys.argv[1:]
c, a, d, m, r = [x == "true" for x in f]
st = {"timestamp": datetime.datetime.now().astimezone().isoformat(),
      "archive": arch, "size_bytes": os.path.getsize(arch) if os.path.exists(arch) else 0,
      "backup": {"created": c, "archive_integrity": a, "database_integrity": d,
                 "manifest_integrity": m, "restore_verified": r}}
st["backup_verified"] = all(st["backup"].values())   # derivado de TODOS == true
json.dump(st, open(p, "w"), indent=2)
print(json.dumps(st["backup"], indent=2)); print("backup_verified:", st["backup_verified"])
PY
# manifiesto final: incluye state.json y RESTORE.md, y nada borrado después
( cd "$DEST" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256 )
( cd "$DEST" && sha256sum -c MANIFEST.sha256 --quiet ) || echo "✗ manifiesto final inconsistente"
chmod -R go-rwx "$DEST"
grep -q '"backup_verified": true' "$STATE"
