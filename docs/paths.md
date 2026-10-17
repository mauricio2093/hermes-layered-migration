# Rutas efectivas de Hermes en el host de producción

Observado el 2026-10-16. **Documentado, no modificado.** Cualquier cambio
requiere autorización.

## Qué ejecuta cada cosa

| Quién | Ejecutable | Código | Versión |
|---|---|---|---|
| **Gateway (systemd)** | `~/.hermes/hermes-oficial-clean/.venv/bin/python hermes_cli/main.py gateway run` | `hermes-oficial-clean` (editable) | v0.21.3+11809 (`5c9c528`) |
| **CLI `hermes` del PATH** | `~/.local/bin/hermes` → `~/.hermes/hermes-agent/venv/bin/python ~/.hermes/hermes-agent/hermes` | **el fósil** `~/.hermes/hermes-agent` | **v0.20.1** (2026.8.13) |
| Lanzador PM | `~/.hermes/hermes-oficial-clean/.hermes/bin/hermes` | — | **roto**: «no dependency environment is committed» |
| hermes-updater | usa `.venv/bin/python` de `hermes-oficial-clean` de forma explícita para `hermes send` y `hermes backup` | `hermes-oficial-clean` | — |
| Onion | `~/hermes-agent` (otro proyecto) | — | no se toca |

El proceso vivo lo confirma: PID con `exe` igual al Python 3.11 de uv,
`cmdline` `…/hermes-oficial-clean/.venv/bin/python …/hermes_cli/main.py gateway run`,
`cwd` `~/.hermes` y ninguna variable `ORCA_*`.

## La unit del gateway

```
FragmentPath  ~/.config/systemd/user/hermes-gateway.service
              ExecStart="…/hermes-oficial-clean/.hermes/bin/hermes" "gateway" "run"   <- lanzador PM roto
DropInPaths   ~/.config/systemd/user/hermes-gateway.service.d/10-legacy-venv-launch.conf
              ExecStart=  (reset)
              ExecStart=…/hermes-oficial-clean/.venv/bin/python …/hermes_cli/main.py gateway run   <- lo que corre
WorkingDirectory ~/.hermes
Environment   PATH=~/.hermes/node/bin:~/.hermes/node:~/.local/bin:…   HERMES_HOME=~/.hermes   HERMES_SUPERVISED_CHILD=1
ExecStopPost  -"…/hermes-oficial-clean/.hermes/bin/hermes" "--run-module" "gateway.cgroup_cleanup"
```

## Riesgos observados (no corregidos)

1. **La CLI `hermes` del usuario es v0.20.1** sobre el mismo `~/.hermes` que el
   gateway 0.21.3. Lo que alguien ejecute a mano (`hermes config …`,
   `hermes update`, `hermes backup`…) corre código de agosto contra una
   configuración y unas bases de un esquema posterior: config v49 frente a la
   v0.20.1, y `state.db` en schema 31 frente al 25 del fósil. Ni el crontab ni
   los scripts de `~/scripts` o `~/.hermes/scripts` la invocan (comprobado);
   el riesgo es el uso manual.
2. **`ExecStopPost` falla siempre** y en silencio (el prefijo `-` lo ignora): el
   journal muestra «no dependency environment is committed…» en cada parada.
   La limpieza de cgroup del gateway (`gateway.cgroup_cleanup`) **no se
   ejecuta**.
3. **El `ExecStart` base sigue siendo el lanzador roto.** Solo el drop-in mantiene
   vivo el gateway. Si el drop-in se pierde, o un `hermes` vuelve a reescribir la
   unit como el 2026-10-01, se repite el bucle de reinicios. El backup diario
   guarda ahora la unit **y** el drop-in en `host/systemd/`.
4. `AGENTS.md` (Onion) cita `~/.hermes/hermes-agent` como «repo oficial»; hoy el
   oficial en producción es `hermes-oficial-clean`.

## Opciones (requieren autorización; ninguna aplicada)

- CLI: que `~/.local/bin/hermes` apunte al venv de `hermes-oficial-clean`
  (como hace el drop-in) o retirarla; conservar el fósil solo para el rollback.
- Unit: llevar el `ExecStart` del drop-in a la unit, o reparar el entorno PM
  (`hermes pm repair`) siguiendo el protocolo de actualización controlada,
  porque puede recrear el venv y perder `hermes_hro`.
- `ExecStopPost`: apuntarlo al mismo `python` del venv
  (`… -m gateway.cgroup_cleanup`) o quitarlo.
