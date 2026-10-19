# `hermes-updater` — orquestador de mantenimiento y actualizaciones

Estado a 2026-10-16: **IMPLEMENTADO y PROBADO en aislamiento. NO DESPLEGADO.**
Ningún timer nuevo está activo, nada se ha instalado en `~/.local/bin`, el
helper root no se ha reinstalado y producción no ha cambiado.

`hermes-maint` sigue siendo lo que era: un **observador** de solo lectura a las
03:00. `hermes-updater` es otra pieza, con otra unit, otro candado y otro
estado. No cambia la semántica ni los códigos de salida de `hermes-maint`.

## Qué hace

```
diario (timer 04:15, o `hermes-updater run --trigger manual`)
  PREFLIGHT            disco, memoria, gateway, última observación de hermes-maint
  DAILY_BACKUP         SIEMPRE, antes de mirar versiones: no depende de la red,
                       de que haya novedades ni de que la detección no falle
  VERSION_DISCOVERY    Hermes (git, sin fetch en producción) + Claude/Codex/OpenCode
  UPDATE_CLASSIFICATION
   ├─ NO_UPDATES → NOTIFY_CURRENT_VERSIONS → COMPLETE
   └─ UPDATES_AVAILABLE → NOTIFY_AVAILABLE
        → AUX_UPDATES          (cada CLI por separado; nunca arrastra a Hermes)
        → BACKUP               (pre-update: el A de ESTA ejecución + ZIP oficial B + código + servicio)
        → BACKUP_VERIFICATION  (verificador independiente + restauración aislada)
        → STAGING_PREPARATION  (clon --shared, merge del tag estable, venv copiado)
        → COMPATIBILITY_TESTS  (suites + smoke con la Capa 3 RESTAURADA)
        → AI_REVIEW            (auditor sin herramientas; solo puede empeorar)
        → UPDATE_READY → AUTHORIZATION_GATE   → PENDING_APPROVAL por Telegram
  TELEGRAM_REPORT

aplicación (solo con aprobación TOTP)
  AUTHORIZATION_GATE → CONTROLLED_DEPLOYMENT → SERVICE_RECOVERY → POST_UPDATE_TESTS
   ├─ PASS → COMPLETE
   └─ FAIL → ROLLBACK → VERIFY_RECOVERY   (si falla: RECOVERY_FAILED = incidente)
  TELEGRAM_REPORT
```

AI_REVIEW va **después** de las pruebas y no antes como en el boceto inicial,
porque el auditor tiene que evaluar sus resultados.

## Fronteras que no se cruzan

| | |
|---|---|
| `hermes update` | **nunca** se ejecuta. El candidato es un `merge` del tag estable sobre `feat/layer2-schema` en un clon aislado; los commits propios no se reescriben. |
| Producción | solo `apply` la toca, y solo con una aprobación escrita **por root** tras verificar TOTP. `run` jamás. |
| Conflictos | `merge --abort`, candidato BLOCKED con la lista exacta de ficheros. No se fuerzan parches. |
| IA | sin herramientas (`claude -p --tools ""`), evidencia redactada en el prompt, esquema cerrado. `pass` no promociona nada; `concerns` → DEFERRED, `block` → BLOCKED; caída → DEFERRED (o BLOCKED con `ai_required`). No puede autorizar privilegios: si los "recomienda", se marca y no tiene efecto. |
| Estados | UNKNOWN / NOT_OBSERVED / INSUFFICIENT_DATA / DEFERRED nunca cuentan como PASS. El store **se niega** a cerrar una ejecución en verde con comprobaciones obligatorias sin PASS. |
| Onion | no se toca. |
| AGENTS.md | no se toca. |

## Backups

**A (diario)** — `backup-hermes-home.sh` sin cambios de lógica (solo
`BACKUP_ROOT` opcional y `hermes-control/gifs.db` declarada). Además del
veredicto del script, `hermes_updater.verify` lo verifica **de forma
independiente** (Python puro, sin `sha256sum`/`tar`/`gzip`, sin leer
`state.json` para decidir):

1. MANIFEST recalculado; nada sin listar.
2. Archivo legible entero con `tarfile`; sin rutas que escapen; sin bases en caliente.
3. Inventario descubierto en el **vivo**: toda base viva tiene copia consistente.
4. Restauración real en un directorio aislado (en disco, no en `/tmp`, que es RAM):
   cada base en su ruta, `quick_check` + `integrity_check`.
5. `plugins/` y `scripts/` idénticos al content-manifest, en las dos direcciones.
6. Permisos de secretos restaurados = esperados.
7. Rutas críticas presentes.

La evidencia queda en `~/.local/state/hermes-updater/evidence/backup-<id>-<sha>.json`.
Al quedar en `backups/independiente/`, `hermes-maint` (backup-freshness) lo ve
y deja de marcar Degraded.

**B (previo a actualización)** — siempre nuevo: A fresco + verificación, ZIP
oficial (`hermes backup`, comprobado con `zipfile`), HEAD/rama/refs, bundle de
los commits propios (`git bundle verify`), `format-patch`, `git status`,
`pip freeze`, unit + drop-ins del gateway (el drop-in
`10-legacy-venv-launch.conf` es lo que lo mantiene vivo), crontab, versiones y
MANIFEST del conjunto. Antes de desplegar se re-verifica el MANIFEST.

**Retención**: solo se podan backups creados por el orquestador, nunca el
último verificado, nunca uno ajeno, nunca un fallo posterior al último bueno.

## Pruebas del candidato

| suite | obligatoria | qué |
|---|---|---|
| `upstream-layer2` | sí | `tests/layer2` + los tests que tocan los commits propios |
| `upstream-core` | sí | `tests/hermes_state`, Telegram, comandos, plugins, migraciones de config |
| `hermes-control` | sí | la suite completa del plugin contra el código candidato |
| `smoke-imports` | sí | gateway, CLI, estado, adaptador Telegram |
| `smoke-config-check` / `smoke-plugins` | sí | con la **Capa 3 restaurada** del backup |
| `smoke-db-migration` | sí | el candidato abre una **copia restaurada** de `state.db`; sesiones/mensajes iguales, integrity ok |

Un fallo solo se perdona si es **preexistente demostrado**: se re-ejecuta en
el tag upstream puro (o, para hermes-control, contra el código de
producción) y también falla allí. Cero tests ejecutados, timeout o
error interno de pytest = FAIL. Todo corre en `systemd-run --scope` con
`MemoryMax=1800M` y `nice`.

- Un módulo que no se puede recoger (`NameError` al importar) ya no detiene
  la suite: pytest corre con `--continue-on-collection-errors` y el módulo
  cuenta como un fallo más, con el fichero como identificador. v0.22.2 trae uno
  así (`tests/hermes_cli/test_plugins_cmd_remove_bookkeeping.py`, también roto
  en el tag puro), y antes dejaba `upstream-core` en 0 tests ejecutados.
- Antes de comparar con la línea base, los fallidos se repiten **una vez** en
  el candidato. Los que pasan quedan como **inestables** (`flaky`): constan en
  el detalle y en la evidencia del auditor IA, no como regresión. En este
  equipo, una suite lanzada con otra en paralelo hacía fallar por tiempo los
  `Writer.flush()` del optimizador; en frío pasan.
- pytest corre bajo `eatmydata` si está instalado: las bases de los tests son
  desechables, y en el disco mecánico de producción cada sentencia DDL de
  SQLite esperaba a un fsync (~37 ms). `upstream-core` de v0.22.2 no terminaba
  en 60 minutos (6 s de preparación por test); con `eatmydata`, el fichero
  medido pasa de 73 s a 3 s. Solo afecta a los procesos de pytest, nunca al
  gateway ni a los backups.
- El venv del candidato es una copia del productivo. Tras copiarlo se borran
  los `*-pytest-*.pyc`: son las cachés de pytest con las aserciones
  reescritas, y al cargarlas su ruta de origen no se corrige. El venv copiado
  seguía apuntando a `~/.hermes/hermes-oficial-clean/.venv`, la protección
  `home_io_guard` de v0.22.2 lo detectaba y pytest moría con INTERNALERROR a
  mitad de `upstream-core`.
- Un INTERNALERROR de pytest es siempre FAIL ("la suite no terminó"): nunca se
  confunde con un test que falla.

## Aprobación (integración con hermes-control)

El secreto TOTP es root-only, así que la única verificación que un proceso del
usuario (o un LLM con terminal) no puede falsificar es la del helper root:

```
Telegram:  ! hermes-update aprobar <código de 24 hex>
  → hermes-control: grant + [Autorizar] + código TOTP     (flujo existente)
  → sudo -n hermes-priv-helper update-approve <código>    (verifica TOTP)
  → /var/lib/hermes-update-approvals/<código>.json         (root:root 0644)
  → hermes-updater-apply.path → hermes-updater apply
```

El código es el sha256 del payload canónico: origen, destino, commit
candidato, backup verificado, resultado de tests, revisión IA, caducidad y
un nonce. Otro candidato → otro código. `apply` vuelve a comprobarlo todo:
dueño root y no escribible por otros, código, aprobación posterior al
candidato y no caducada (30 min), candidato no caducado (72 h), **un solo
uso**, HEAD productivo == origen aprobado, árbol limpio, candidato y lock
intactos, backup pre-update intacto, disco, gateway activo antes.

## Despliegue y rollback

Punto de rollback **persistido antes** de tocar nada: HEAD viejo, copia del
`.venv` (`cp -a --reflink=auto`), snapshot consistente de todas las bases,
copia de unit + drop-ins. Código: `fetch` del clon de staging + `merge
--ff-only`. Dependencias: solo las líneas del lock probado que difieren
(`--no-deps`) y el editable. **Un** reinicio. Sano = activo + mismo PID
`service_stable_s` + sin reinicios nuevos.

Post-pruebas: HEAD == candidato; `get_version_info` == versión destino; logs
sin Traceback/CRITICAL desde el reinicio; `ExecStart` sin cambios (drop-in
intacto); `hermes_control` enabled; `state.db` quick_check y conteos ≥ antes;
`config check`; capas externas presentes y `plugins/`/`scripts/` idénticos;
ruta de Telegram probada con un envío real (si se autoriza el despliegue).

Rollback (automático, **suave**): `reset --hard` al HEAD viejo, `.venv`
anterior de vuelta, unit restaurada si cambió, un reinicio, verificación.
La base se conserva (el runbook reserva el hard para una decisión). Si la
recuperación no deja el servicio sano: `RECOVERY_FAILED`, incidente por
Telegram, se detiene todo.

Rollback manual:

```
hermes-updater rollback --run <N>          # suave
hermes-updater rollback --run <N> --hard   # además: para el gateway y restaura TODAS las
                                           # bases del snapshot pre-despliegue (la actual
                                           # queda en db-displaced-*)
```

Una ejecución interrumpida en un estado de despliegue se **reconcilia** en
la siguiente invocación: post-pruebas; si no pasan, rollback y verificación.

## Herramientas auxiliares

| | instalación detectada | mecanismo | recuperación |
|---|---|---|---|
| Claude Code | nativa, `~/.local/share/claude/versions` | `claude update` | symlink a la versión anterior (se conserva) |
| Codex | standalone, `~/.codex/packages/standalone` | `codex update` | symlink `current` anterior |
| OpenCode | curl, `~/.opencode/bin` | `opencode upgrade --method curl` | copia del binario |

Saltos de versión mayor → DEFERRED. OpenCode con procesos vivos → DEFERRED
(reemplaza el binario; Claude y Codex instalan por versión y no afectan a
procesos vivos). Nunca Claude desde dentro de Claude. Sin sudo. Tras el
comando: código de salida + versión posterior + `--version`/`--help`; si algo
falla, se restaura y se comprueba. Las tres ya se auto-actualizan solas (hoy
lo hicieron a las 07:02 y 18:34); el orquestador cubre los días en que no.

## Primer despliegue real de v0.22.2 (2026-10-19): revertido, y por qué

El despliegue se revirtió solo y dejó producción como estaba (HEAD, venv, unit,
gateway activo). Las dos post-pruebas que fallaron eran defectos del
actualizador, no de v0.22.2:

- **Versión informada 0.21.3.** Hermes calcula su versión con los tags `v*`
  alcanzables desde HEAD, y el despliegue traía la rama del candidato sin el
  tag. Ahora trae `refs/tags/vX` y comprueba que apunta al commit aprobado.
- **"La unit cambió".** Se comparaba la línea entera de `systemctl show -p
  ExecStart`, que lleva pid y hora de arranque. Ahora solo ruta y argv. La unit
  y el drop-in eran idénticos byte a byte al backup.

Y dos debilidades que el incidente dejó a la vista:

- **Sano no es solo "activo 20 s".** v0.22.2 superó esa marca sin haberse
  conectado aún a Telegram. Ahora hace falta además `Connected to Telegram` en
  el journal después del reinicio (`service_ready_log`); el margen pasa a
  300 s.
- **El rollback cambiaba el código con el gateway vivo.** El proceso de v0.22.2,
  que aún arrancaba, importó un módulo ya revertido (`ModuleNotFoundError:
  agent.session_source`). Ahora el rollback para el gateway, restaura código,
  venv y unit, y lo arranca.

## Resoluciones de conflicto revisadas

Un merge con conflictos se aborta y el candidato queda BLOCKED, salvo que el
conflicto sea **idéntico** a uno ya resuelto y revisado por una persona. Esas
resoluciones viven en `~/.local/state/hermes-updater/resolutions/rr-cache`
(formato de `git rerere`: la huella del conflicto, `preimage`, y cómo quedó,
`postimage`). El clon las copia; nunca escribe en ellas.

- Se reutilizan solo si `git rerere remaining` queda vacío y ningún fichero
  conserva marcas de conflicto. Un conflicto distinto, o uno resuelto a medias,
  sigue bloqueando.
- El commit del candidato lo dice ("Conflicts resolved by reviewed recorded
  resolutions") y el detalle también: nunca aparece como "merge limpio".
- El auditor IA recibe la lista de ficheros y el diff combinado
  (`git show --cc`) de lo resuelto. Las pruebas corren igual que siempre.

Grabar una resolución: repetir el merge en un clon con `rerere.enabled`,
resolver, revisar, `git commit`, y copiar su `.git/rr-cache` a ese directorio.

## Notificaciones

Por `hermes send --to telegram` (CLI oficial de Hermes para scripts y cron;
reutiliza las credenciales del gateway, sin LLM, sin necesitar el gateway
vivo). No hay bot alternativo. Todo mensaje pasa antes por una bandeja SQLite
con clave de deduplicación; si Telegram falla, se reintenta en la siguiente
ejecución y nunca bloquea una recuperación.

## Instalación, activación y desactivación

Nada de esto se ha ejecutado. Requiere tu autorización.

```bash
# 1) instalar el orquestador (copia, no el checkout del repo)
install -d ~/.local/lib/hermes-updater
cp -r ~/repos/hermes-layered-migration/updater/{bin,hermes_updater} ~/.local/lib/hermes-updater/
ln -sf ~/.local/lib/hermes-updater/bin/hermes-updater ~/.local/bin/hermes-updater

# 2) declaraciones (Capa 3) y script de backup: JUNTOS, ver docs/backup-decl-007.md
cp ~/.hermes/backup-declarations.sh ~/.hermes/backup-declarations.sh.bak-$(date +%Y%m%d)
cp <propuesta revisada> ~/.hermes/backup-declarations.sh
cp ~/repos/hermes-layered-migration/scripts/backup-hermes-home.sh ~/.hermes/scripts/

# 2b) resoluciones revisadas (hoy: la de v0.22.2 en telegram/adapter.py)
install -d ~/.local/state/hermes-updater/resolutions
cp -a ~/.local/state/hermes-updater-dev/state/resolutions/rr-cache ~/.local/state/hermes-updater/resolutions/

# 3) prueba manual sin esperar al timer (no toca producción)
hermes-updater run --trigger manual --no-aux --notify file

# 4) timer diario
cp ~/repos/hermes-layered-migration/systemd/hermes-updater.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now hermes-updater.timer

# 5) aprobación por Telegram (root): reinstalar el helper de la rama
#    feat/update-approve de hermes-control y desplegar el plugin; luego
cp ~/repos/hermes-layered-migration/systemd/hermes-updater-apply.{path,service} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now hermes-updater-apply.path
```

Desactivar: `systemctl --user disable --now hermes-updater.timer hermes-updater-apply.path`.

## Diagnóstico

```bash
hermes-updater versions                 # las cuatro versiones, sin tocar nada
hermes-updater status                   # últimas ejecuciones, candidato pendiente, bandeja
hermes-updater checks --run N           # cada comprobación con su veredicto y detalle
hermes-updater flush                    # reenviar notificaciones pendientes
journalctl --user -u hermes-updater.service -n 100
sqlite3 ~/.local/state/hermes-updater/updater.db 'select id,kind,state,outcome from runs order by id desc limit 10'
```

Códigos de salida: 0 SUCCESS/NO_UPDATES · 3 candado ocupado · 4 PENDING_APPROVAL ·
6 BLOCKED · 7 FAILED · 8 ROLLED_BACK · 9 RECOVERY_FAILED. La unit acepta 3 y 4
como éxito; 6–9 son hallazgos y se ven en rojo.

## Riesgos residuales

- El servicio en los tests de cutover/rollback es simulado; git, el
  intercambio de venv y los backups son reales. El primer despliegue real
  será la primera observación en producción.
- La ruta real de Telegram no se ha probado enviando (requiere tu permiso).
- v0.22.2 choca con la Capa 2 en `plugins/platforms/telegram/adapter.py`:
  upstream pasó dos firmas a genéricos nativos (`dict`/`list`) y el commit de
  los botones de copiar añadió parámetros en esas mismas líneas. Se resolvió a
  mano (genéricos nuevos y parámetros propios) y la resolución se guardó como
  **resolución revisada** (ver abajo). Con ella, `prepare()` produce el árbol
  revisado byte a byte. La partición de `hermes_cli/plugins.py` en
  `plugins_*.py` no rompe `61479f3985`: usa los métodos de la clase base, que
  siguen existiendo; lo confirman sus tests en el candidato.
- La verificación del tag es por coincidencia con `ls-remote`; upstream no
  firma los tags (`NOT_OBSERVED`).
- `pip install` en el despliegue descarga de PyPI; sin red, el despliegue
  falla y hace rollback.
