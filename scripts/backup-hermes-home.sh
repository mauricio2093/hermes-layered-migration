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
# BACKUP_ROOT: solo para el orquestador y los tests (destino aislado). Por
# defecto, el de siempre: es el que lee hermes-maint/backup-freshness.
DEST="${BACKUP_ROOT:-$HERMES_HOME/backups/independiente}/$TS"
DBDIR="$DEST/db-consistent"
HOSTDIR="$DEST/host"
ARCHIVE="$DEST/hermes-home-$TS.tar.gz"
STATE="$DEST/state.json"
# Lo que vive FUERA de HERMES_HOME y hace falta para reconstruir Hermes.
# Sobrescribibles solo para los tests (un HERMES_HOME falso con su propio
# checkout y su propio directorio de units).
PROD_REPO="${HERMES_PROD_REPO:-$HERMES_HOME/hermes-oficial-clean}"
SYSTEMD_USER_DIR="${HERMES_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
GATEWAY_UNIT="${HERMES_GATEWAY_UNIT:-hermes-gateway.service}"
PROD_PYTHON="${HERMES_PROD_PYTHON:-$PROD_REPO/.venv/bin/python}"
EXTRA_REPOS="${HERMES_EXTRA_REPOS:-$HOME/repos/hermes-control $HOME/repos/hermes-layered-migration}"

# --- Declaraciones POR NOMBRE ------------------------------------------------
# Historia: plugins/ y scripts/ pasaron de recuento a nombre + sha256 el
# 2026-09-27; patches/, anchors/ y voice-samples/ el 2026-10-16.
#
# El recuento (patches=6 anchors=27 voice-samples=7) no distinguia un fichero
# perdido compensado por uno nuevo, no veia la corrupcion, y fallaba con
# cualquier cambio legitimo: el 2026-10-16 anchors tenia 30 (tres documentos
# de anclaje nuevos, del 3 y del 12 de octubre) y la unica salida era subir el
# numero sin mirar. Ahora:
#   - se declaran los NOMBRES; uno no declarado aborta, igual que una base no
#     declarada. Declararlo es haberlo revisado;
#   - el contenido viaja con content-manifest.sha256, y el restore lo comprueba
#     en las dos direcciones: nada falta, nada sobra, nada cambia.
# Los nombres viven FUERA de este repo, en $HERMES_HOME/backup-declarations.sh
# (dentro del backup): son de la instalacion, no del codigo, y este repo es
# publico. Sin ese fichero, o sin una de sus listas, no hay declaracion y el
# backup no se da por bueno.
#
#   DECL_PLUGINS        plugins/<nombre> (directorios con plugin.yaml)
#   DECL_SCRIPTS        ficheros de scripts/
#   DECL_PATCHES        ficheros de patches/
#   DECL_ANCHORS        ficheros de anchors/ (sin bases: van en db-consistent/)
#   DECL_VOICE          ficheros de voice-samples/
#   DECL_SQLITE_COLD    bases SQLite ARCHIVADAS (no vivas) que viajan en el tar
#                       tal cual, p. ej. state.db.bak-3504 de una migracion
CONTENT_DIRS=(plugins scripts patches anchors voice-samples)
DECL_FILE="$HERMES_HOME/backup-declarations.sh"
if [ -f "$DECL_FILE" ]; then
  # shellcheck source=/dev/null
  . "$DECL_FILE"
fi
# 6 -> 8 (2026-09-18, BACKUP-DECL-006). Hermes 0.21.3 trajo dos bases nuevas que
# no capturaba nadie: el tar excluye *.db y no estaban declaradas aqui, asi que
# habrian faltado en silencio. Entran por la misma via que las demas -- copia
# consistente con Connection.backup() en db-consistent/ e integrity_check --
# sin excepciones:
#   shared-state.db       Hosted Rooms / coordinacion durable
#   cron/deliveries.db    cola durable de entregas cron + tombstones
# 8 -> 9 (2026-09-30). costs/prices.db: catalogo de precios del Cost Ledger
# (hermes_control). Se regenera desde models.dev, pero guarda overrides de precio
# hechos a mano y la fecha del catalogo con la que se congelaron los snapshots:
# es estado, no cache. Mismo camino que las demas, sin excepciones.
# 9 -> 10 (2026-10-16). hermes-control/gifs.db: cache de GIFs ya entendidos
# (hermes_control/gifs, desplegado 2026-10-06). Guarda descripciones y hashes
# perceptuales aprendidos; perderlo obliga a volver a pagar la vision de cada
# GIF. Lo detecto el propio invariante de "bases no declaradas" de abajo.
DBS=(state.db verification_evidence.db kanban.db shared-state.db cron/executions.db cron/deliveries.db cron/notepad.db anchors/data/phone.db costs/prices.db hermes-control/gifs.db)
SECRETS=(.env auth.json config.yaml channel_directory.json)

# --- Cobertura: UNA sola definicion para el tar y para el inventario ----------
# Antes el tar excluia una lista y la busqueda de bases otra (p. ej. el tar
# metia hermes-agent/ pero la busqueda lo saltaba): una base ahi dentro habria
# faltado sin aviso. Ahora las dos salen de aqui. El verificador independiente
# (hermes_updater/verify.py) tiene su copia y un test exige que coincidan.
EXCLUDE_TOP=(backups rescue hermes-oficial-clean node lsp cache audio_cache)
EXCLUDE_SUB=(hermes-agent/venv hermes-agent/.git hermes-agent/node_modules)

ok_created=false; ok_archive=false; ok_db=false; ok_manifest=false; ok_restore=false; ok_host=false
decl_ok=true
step() { echo; echo "── $* ──"; }
# El mismo filtro en el vivo, en el manifiesto y en el restore: lo que el tar
# excluye a proposito (__pycache__, *.pyc) no cuenta en ninguno de los tres.
# Las bases VIVAS declaradas (anchors/data/phone.db) tampoco: viajan en
# db-consistent/. Se excluyen por RUTA EXACTA, nunca por patron: un "x.db"
# que no sea una base declarada sigue en el manifiesto y, como el tar no lo
# trae, el restore lo da por perdido. Nada desaparece en silencio.
live_db_paths() { for db in "${DBS[@]}"; do printf '%s\n%s-wal\n%s-shm\n' "$db" "$db" "$db"; done; }
content_files() { ( cd "$1" && find "${CONTENT_DIRS[@]}" -type f ! -path '*__pycache__*' ! -name '*.pyc' 2>/dev/null \
  | grep -vxF -f <(live_db_paths) | LC_ALL=C sort ); }
same_set() {  # $1 etiqueta, $2 declarados (uno por linea), $3 en disco
  local faltan sobran
  faltan=$(comm -23 <(printf '%s\n' "$2" | LC_ALL=C sort -u) <(printf '%s\n' "$3" | LC_ALL=C sort -u) | sed '/^$/d')
  sobran=$(comm -13 <(printf '%s\n' "$2" | LC_ALL=C sort -u) <(printf '%s\n' "$3" | LC_ALL=C sort -u) | sed '/^$/d')
  if [ -z "$faltan$sobran" ]; then
    printf '   ✓ %-26s %s declarados, los mismos en disco\n' "$1" "$(printf '%s\n' "$2" | sed '/^$/d' | wc -l)"
  else
    echo "   ✗ ABORT: $1 no coincide con lo declarado"
    [ -n "$faltan" ] && printf '      falta:        %s\n' $faltan
    [ -n "$sobran" ] && printf '      no declarado: %s\n' $sobran
    decl_ok=false
  fi
}
# En el shell PRINCIPAL (no en $(...)): si no, el decl_ok=false se perderia en
# un subshell y la lista ausente pasaria como vacia, en silencio.
need_decl() {  # $1 nombre de la lista: falla si NO esta definida (vacia si vale)
  declare -p "$1" >/dev/null 2>&1 && return 0
  echo "   ✗ ABORT: $DECL_FILE no define $1"; decl_ok=false; return 1
}
lines_of() { local -n arr="$1"; printf '%s\n' "${arr[@]}"; }
files_in() { ( cd "$HERMES_HOME" 2>/dev/null && find "$1" -type f ! -path '*__pycache__*' ! -name '*.pyc' 2>/dev/null \
  | grep -vxF -f <(live_db_paths) | sed "s|^$1/||" ); }
flat() { echo "$1" | tr '/' '_'; }

mkdir -p "$DBDIR" "$HOSTDIR" || exit 1; chmod 700 "$DEST"

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
expected_databases=${#DBS[@]}
consistent_backups=$(find "$DBDIR" -type f -name '*.db' | wc -l)
echo "   expected_databases=$expected_databases  consistent_backups=$consistent_backups"
if [ "$consistent_backups" -ne "$expected_databases" ]; then
  echo "   ✗ ABORT: faltan copias consistentes"; db_ok=false
fi
# Inventario SQLite POR CONTENIDO (cabecera "SQLite format 3"), no por nombre,
# y sobre exactamente lo que el tar cubre. Una base se llame como se llame
# (x.sqlite, state.db.bak-3504...) debe estar declarada: viva en DBS o
# archivada en DECL_SQLITE_COLD. Si no, ABORT: nunca falta ni viaja en
# caliente sin aviso.
cold_list=""
if need_decl DECL_SQLITE_COLD; then cold_list="$(lines_of DECL_SQLITE_COLD)"; else db_ok=false; fi
inv=$(python3 - "$HERMES_HOME" "${EXCLUDE_TOP[*]}" "${EXCLUDE_SUB[*]}" "${DBS[*]}" "$cold_list" <<'PY'
import os, sys, sqlite3
home, top, sub, live, cold = sys.argv[1], set(sys.argv[2].split()), set(sys.argv[3].split()), set(sys.argv[4].split()), set(sys.argv[5].split())
bad = 0
found = []
for root, dirs, files in os.walk(home):
    rel = os.path.relpath(root, home)
    keep = []
    for d in dirs:
        r = d if rel == "." else f"{rel}/{d}"
        if (rel == "." and d in top) or r in sub or d == "__pycache__":
            continue
        keep.append(d)
    dirs[:] = keep
    for f in files:
        p = os.path.join(root, f)
        r = os.path.relpath(p, home)
        try:
            if os.path.islink(p) or os.path.getsize(p) < 100:
                continue
            with open(p, "rb") as fh:
                if fh.read(16) != b"SQLite format 3\x00":
                    continue
        except OSError:
            continue
        found.append(r)
for r in sorted(found):
    if r in live:
        continue
    if r in cold:
        wal = os.path.join(home, r + "-wal")
        # Un -wal VACIO (o un -shm) lo deja cualquier apertura de lectura de una
        # base en modo WAL; no prueba un escritor. Uno con contenido o un
        # -journal, si: entonces no es una base archivada.
        if (os.path.exists(wal) and os.path.getsize(wal) > 0) or os.path.exists(os.path.join(home, r + "-journal")):
            print(f"   ✗ ABORT: {r} esta declarada ARCHIVADA pero tiene WAL con datos o journal: es una base viva"); bad = 1
            continue
        try:
            # immutable=1: no bloquea ni crea -wal/-shm junto a una base archivada
            c = sqlite3.connect(f"file:{os.path.join(home, r)}?mode=ro&immutable=1", uri=True)
            ic = c.execute("PRAGMA integrity_check").fetchone()[0]; c.close()
        except sqlite3.Error as e:
            ic = str(e)
        print(f"   {'✓' if ic == 'ok' else '✗'} archivada {r}: integrity={ic}")
        bad |= ic != "ok"
        continue
    print(f"   ✗ ABORT: base SQLite NO declarada: {r}"); bad = 1
for c in sorted(cold - set(found)):
    print(f"   ✗ ABORT: declarada archivada pero no existe: {c}"); bad = 1
print(f"   inventario SQLite: {len(found)} bases ({len(set(found) & live)} vivas, {len(set(found) & cold)} archivadas)")
sys.exit(bad)
PY
)
inv_rc=$?
echo "$inv"
[ "$inv_rc" -eq 0 ] || db_ok=false
$db_ok && ok_db=true

step "2b. Declaraciones por nombre (plugins, scripts, patches, anchors, voice-samples)"
[ -f "$DECL_FILE" ] || { echo "   ✗ ABORT: falta $DECL_FILE"; decl_ok=false; }
need_decl DECL_PLUGINS && same_set "plugins" "$(lines_of DECL_PLUGINS)" \
  "$(cd "$HERMES_HOME/plugins" 2>/dev/null && find . -name plugin.yaml ! -path '*__pycache__*' | sed 's|^\./||; s|/plugin.yaml$||')"
need_decl DECL_SCRIPTS && same_set "scripts" "$(lines_of DECL_SCRIPTS)" "$(files_in scripts)"
need_decl DECL_PATCHES && same_set "patches" "$(lines_of DECL_PATCHES)" "$(files_in patches)"
need_decl DECL_ANCHORS && same_set "anchors" "$(lines_of DECL_ANCHORS)" "$(files_in anchors)"
need_decl DECL_VOICE && same_set "voice-samples" "$(lines_of DECL_VOICE)" "$(files_in voice-samples)"
# Manifiesto del contenido, tomado del VIVO justo antes de empaquetar.
content_files "$HERMES_HOME" | ( cd "$HERMES_HOME" && tr '\n' '\0' | xargs -0 -r sha256sum ) > "$DEST/content-manifest.sha256"
echo "   content-manifest: $(wc -l < "$DEST/content-manifest.sha256") ficheros de ${CONTENT_DIRS[*]}"

step "2c. Estado del host (lo que vive fuera de HERMES_HOME)"
# Sin esto un backup restaura los datos pero no el Hermes que los usa: los
# commits propios (Capa 2) solo existen en este disco; el gateway arranca
# gracias a un drop-in; el wheel de hermes_hro no esta en ningun git.
host_ok=true
hfail() { echo "   ✗ $*"; host_ok=false; }
mkdir -p "$HOSTDIR/systemd" "$HOSTDIR/wheels"
if [ -f "$SYSTEMD_USER_DIR/$GATEWAY_UNIT" ]; then
  ( cd "$SYSTEMD_USER_DIR" && find . -maxdepth 2 \( -name 'hermes-*.service' -o -name 'hermes-*.timer' -o -name 'hermes-*.path' \
      -o -path "./$GATEWAY_UNIT.d/*.conf" \) -print0 | xargs -0 -r cp --parents -t "$HOSTDIR/systemd" )
  echo "   ✓ units: $(cd "$HOSTDIR/systemd" && find . -type f | sed 's|^\./||' | LC_ALL=C sort | tr '\n' ' ')"
else
  hfail "no existe la unit $SYSTEMD_USER_DIR/$GATEWAY_UNIT"
fi
crontab -l > "$HOSTDIR/crontab.txt" 2>/dev/null || echo "# sin crontab para $(id -un)" > "$HOSTDIR/crontab.txt"
if git -C "$PROD_REPO" rev-parse HEAD >/dev/null 2>&1; then
  g() { git -C "$PROD_REPO" "$@"; }
  head=$(g rev-parse HEAD); branch=$(g rev-parse --abbrev-ref HEAD); up=$(g rev-parse -q --verify refs/remotes/origin/main || true)
  { echo "repo $PROD_REPO"; echo "HEAD $head"; echo "branch $branch"; echo "origin/main $up"; } > "$HOSTDIR/prod-refs.txt"
  g status --porcelain > "$HOSTDIR/prod-status.txt"
  ncar=$(g rev-list --count --no-merges HEAD ${up:+^$up})
  if [ -z "$up" ]; then
    hfail "sin refs/remotes/origin/main: no se puede acotar el bundle de commits propios"
  elif [ "$ncar" -eq 0 ]; then
    echo "   ✓ commits propios: 0 (nada que empaquetar)"
  elif g bundle create "$HOSTDIR/carried.bundle" "$up..HEAD" >/dev/null 2>&1 \
       && g bundle verify "$HOSTDIR/carried.bundle" >/dev/null 2>&1; then
    echo "   ✓ commits propios: $ncar en carried.bundle (git bundle verify ok; prerrequisito upstream ${up:0:10})"
  else
    hfail "no se pudo crear/verificar el bundle de los $ncar commits propios"
  fi
  [ -s "$HOSTDIR/prod-status.txt" ] && echo "   ! produccion tiene cambios sin commit (listados en host/prod-status.txt)"
  py="$PROD_PYTHON"
  if [ -x "$py" ] && "$py" -m pip freeze --all > "$HOSTDIR/venv-freeze.txt" 2>/dev/null && [ -s "$HOSTDIR/venv-freeze.txt" ]; then
    echo "   ✓ pip freeze del venv: $(wc -l < "$HOSTDIR/venv-freeze.txt") paquetes"
    # paquetes instalados desde un fichero local (p. ej. el wheel de hermes_hro):
    # sin copia, no se pueden reinstalar.
    "$py" - "$HOSTDIR/wheels" <<'PY' || host_ok=false
import hashlib, json, shutil, sys
from importlib import metadata
from pathlib import Path
out = Path(sys.argv[1]); bad = 0
for dist in metadata.distributions():
    raw = dist.read_text("direct_url.json")
    if not raw:
        continue
    d = json.loads(raw)
    url = d.get("url", "")
    if "archive_info" not in d or not url.startswith("file://"):
        continue
    src = Path(url[7:]); want = (d["archive_info"].get("hashes") or {}).get("sha256")
    name = dist.metadata["Name"]
    if not src.is_file():
        print(f"   ✗ {name}: instalado desde {src}, que ya no existe"); bad = 1; continue
    have = hashlib.sha256(src.read_bytes()).hexdigest()
    if want and have != want:
        print(f"   ✗ {name}: {src.name} no es el instalado (sha256 distinto)"); bad = 1; continue
    shutil.copy2(src, out / src.name)
    print(f"   ✓ wheel local {src.name} copiado (sha256 = el instalado)")
sys.exit(bad)
PY
  else
    hfail "sin pip freeze del venv productivo ($py)"
  fi
else
  hfail "no hay checkout productivo en $PROD_REPO"
fi
{ for r in $EXTRA_REPOS; do
    [ -d "$r/.git" ] || continue
    echo "== $r"; git -C "$r" status -sb | head -1; git -C "$r" rev-parse HEAD
    git -C "$r" for-each-ref --format='%(refname:short) %(objectname:short) %(upstream:short) %(upstream:track)' refs/heads
  done; } > "$HOSTDIR/repos.txt" 2>/dev/null
{ for b in "$HOME/.local/bin/hermes" "$HOME/.local/bin/hermes-maint" /usr/local/libexec/hermes-priv-helper; do
    [ -e "$b" ] || { echo "ausente $b"; continue; }
    echo "$(sha256sum "$b" | cut -c1-64) $(stat -c '%U:%G %a' "$b") $b -> $(readlink -f "$b")"
  done
  # Solo metadatos: el secreto TOTP y sudoers son de root y NO se copian.
  for f in /etc/sudoers.d/hermes-priv /etc/hermes-priv/totp.key; do
    stat -c '%U:%G %a %n' "$f" 2>/dev/null || echo "no visible para $(id -un): $f"
  done; } > "$HOSTDIR/launchers.txt"
[ -f "$HOME/.local/bin/hermes" ] && cp "$HOME/.local/bin/hermes" "$HOSTDIR/local-bin-hermes"
$host_ok && ok_host=true

step "3+4. Empaquetar (excluye regenerables y las DB en caliente)"
excl=()
for d in "${EXCLUDE_TOP[@]}"; do excl+=(--exclude="$BASE/$d"); done
for d in "${EXCLUDE_SUB[@]}"; do excl+=(--exclude="$BASE/$d"); done
tar -czf "$ARCHIVE" -C "$(dirname "$HERMES_HOME")" "${excl[@]}" \
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

# contenido declarado: el restaurado debe ser EXACTAMENTE el manifiesto.
if ( cd "$H" && sha256sum -c --quiet "$DEST/content-manifest.sha256" ) >/dev/null 2>&1; then
  sobran=$(comm -13 <(cut -c67- "$DEST/content-manifest.sha256" | LC_ALL=C sort) <(content_files "$H"))
  if [ -z "$sobran" ]; then
    printf '   ✓ %-26s %s ficheros, sha256 identico\n' "contenido declarado" "$(wc -l < "$DEST/content-manifest.sha256")"
  else
    echo "   ✗ restaurado trae ficheros fuera del manifiesto:"; printf '      %s\n' $sobran; restore_ok=false
  fi
else
  echo "   ✗ ${CONTENT_DIRS[*]}: el restaurado no coincide con el manifiesto"
  ( cd "$H" && sha256sum -c --quiet "$DEST/content-manifest.sha256" 2>&1 | sed 's/^/      /' | head -10 )
  restore_ok=false
fi
$decl_ok || { echo "   ✗ declaraciones por nombre fallidas (paso 2b)"; restore_ok=false; }

# bases archivadas: viajan en el tar; la copia restaurada tambien debe abrir
for c in $cold_list; do
  if [ -f "$H/$c" ]; then
    r=$(python3 -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" "$H/$c" 2>&1)
    [ "$r" = "ok" ] && printf '   ✓ %-26s archivada, integrity=ok\n' "$c" \
                    || { printf '   ✗ %-26s archivada, integrity=%s\n' "$c" "$r"; restore_ok=false; }
  else printf '   ✗ %-26s archivada AUSENTE del archivo\n' "$c"; restore_ok=false; fi
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
{ echo "# Restaurar este backup"
  echo
  echo "    tar -xzf $(basename "$ARCHIVE") -C /destino"
  echo "    # las DB NO están en el tar; copiarlas desde db-consistent/ (${#DBS[@]}):"
  for db in "${DBS[@]}"; do
    echo "    mkdir -p \"/destino/$BASE/$(dirname "$db")\" && cp db-consistent/$(flat "$db") /destino/$BASE/$db"
  done
  echo
  echo "Código y servicio (host/): carried.bundle (commits propios; prerrequisito"
  echo "upstream en prod-refs.txt), venv-freeze.txt + wheels/, systemd/ (incluido"
  echo "el drop-in del gateway), crontab.txt, launchers.txt (solo metadatos de root:"
  echo "el TOTP y sudoers NO están aquí; se reprovisionan)."
  echo
  echo "Verificar antes: sha256sum -c MANIFEST.sha256"; } > "$DEST/RESTORE.md"
python3 - "$STATE" "$ok_created" "$ok_archive" "$ok_db" "$ok_manifest" "$ok_restore" "$ok_host" "$ARCHIVE" <<'PY'
import json, sys, os, datetime
p, *f, arch = sys.argv[1:]
c, a, d, m, r, h = [x == "true" for x in f]
st = {"timestamp": datetime.datetime.now().astimezone().isoformat(),
      "archive": arch, "size_bytes": os.path.getsize(arch) if os.path.exists(arch) else 0,
      # hermes-maint lee los cinco primeros; host_state entra en backup_verified.
      "backup": {"created": c, "archive_integrity": a, "database_integrity": d,
                 "manifest_integrity": m, "restore_verified": r, "host_state": h}}
st["backup_verified"] = all(st["backup"].values())   # derivado de TODOS == true
json.dump(st, open(p, "w"), indent=2)
print(json.dumps(st["backup"], indent=2)); print("backup_verified:", st["backup_verified"])
PY
# manifiesto final: incluye state.json y RESTORE.md, y nada borrado después
( cd "$DEST" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256 )
( cd "$DEST" && sha256sum -c MANIFEST.sha256 --quiet ) || echo "✗ manifiesto final inconsistente"
chmod -R go-rwx "$DEST"
grep -q '"backup_verified": true' "$STATE"
