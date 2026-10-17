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
  VERSION_DISCOVERY    Hermes (git, sin fetch en producción) + Claude/Codex/OpenCode
  UPDATE_CLASSIFICATION
   ├─ NO_UPDATES → DAILY_BACKUP → NOTIFY_CURRENT_VERSIONS → COMPLETE
   └─ UPDATES_AVAILABLE → NOTIFY_AVAILABLE
        → AUX_UPDATES          (cada CLI por separado; nunca arrastra a Hermes)
        → BACKUP               (pre-update: A fresco + ZIP oficial B + código + servicio)
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

# 2) declaraciones pendientes (Capa 3) y script de backup actualizado
#    - añadir orca-status a DECL_PLUGINS y lang-guard.py a DECL_SCRIPTS en
#      ~/.hermes/backup-declarations.sh (después de revisarlos)
#    - anchors: 30 ficheros, el script espera 27 -> revisar y actualizar EXPECT
cp ~/repos/hermes-layered-migration/scripts/backup-hermes-home.sh ~/.hermes/scripts/

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
- Un candidato real de v0.22.2 hoy **no** se puede preparar: conflicto en
  `plugins/platforms/telegram/adapter.py`, y upstream partió
  `hermes_cli/plugins.py` en `plugins_*.py`, lo que afecta a `61479f3985`.
  Necesita una adaptación manual de la Capa 2.
- La verificación del tag es por coincidencia con `ls-remote`; upstream no
  firma los tags (`NOT_OBSERVED`).
- `pip install` en el despliegue descarga de PyPI; sin red, el despliegue
  falla y hace rollback.
