# Layer 2 en observación: qué pasó en 24 h y qué falta

Auditoría de solo lectura, 2026-09-19 20:35, 25 h después del despliegue.
Nada se modificó para hacerla: sin escrituras, sin `VACUUM`, sin migraciones,
sin reinicios.

```
estabilidad del despliegue     PASS
validación funcional           PARCIAL
  command_usage                NOT_OBSERVED
  segunda evaluación del menú  NOT_OBSERVED
```

Sin merge a `main`. Fossil intacto. Onion apagado.

---

## 1. Lo que sí quedó demostrado

| | evidencia |
|---|---|
| gateway estable | `active/running`, `MainPID 1282588` sin cambiar, `NRestarts=0`, uptime 1d 01:31, `Result=success` |
| sin errores nuevos | 0 `ERROR`, 0 `CRITICAL`, 0 tracebacks, 0 menciones a `layer2` en el journal |
| warnings | 27, todos de categorías preexistentes: 16 `auxiliary_client` (openrouter/nous), 7 de red de Telegram, 2 `Thread 3790 not found`, 1 colisión de la skill `plan` |
| base de datos | `integrity_check ok`, `schema_version 30` **sin mover**, 27 tablas = 25 previas + exactamente las 2 esperadas |
| tablas nuevas | `hermes_layer2_schema`, `layer2_telegram_menu_state`, y ninguna más |
| sin divergencia | último checkout 2026-09-16 23:43 < arranque del proceso 2026-09-18 19:04; árbol limpio en `feat/layer2-schema @ 3480077eb6` |
| timer | `active (waiting)`, `LAST 2026-09-19 03:01:10`, `NEXT 2026-09-20 03:00:28`; run 15 `trigger=timer`, tres observaciones `Ok` |

El `status=1` del proceso **viejo** al recibir `SIGTERM` durante el despliegue
es anterior al código nuevo y ya estaba documentado. No cuenta como regresión.

### 1.1 Una propiedad de diseño, verificada contra producción

140 renders del completer del CLI produjeron **una sola lectura SQLite**, y
cambiar el conjunto de comandos produjo una más. Después de todo eso, la base
seguía con 27 tablas, un único componente migrado e `integrity ok`.

Es decir: **el camino de lectura no crea esquema ni escribe**. Era una
afirmación del diseño; ahora es una observación.

## 2. Lo que la prueba no pudo demostrar, y por qué

25 horas con **cero comandos ejecutados** y **una sola conexión a Telegram**
significan que los dos caminos de aprendizaje no han tenido exposición alguna.

Lo verificado es que el despliegue es **inocuo**. Lo que no está verificado es
que **funcione**. Son cosas distintas y conviene no confundirlas al leer el
`PASS` de arriba.

### 2.1 `command_usage` — `NOT_OBSERVED`

`layer2_command_usage` **no existe**, y eso es correcto: solo
`record_command_use` crea la tabla. Que no exista es la prueba directa de que
nadie ha ejecutado un comando por CLI ni por Telegram desde el despliegue.

**Cómo se cierra**, con uso real y sin fabricar nada:

```sql
select command, surface, use_count, score, last_used_at
  from layer2_command_usage;
select component, version from hermes_layer2_schema;
--   debe aparecer  command_usage  v1
```

Pasa si hay al menos una fila cuyo `use_count` y `last_used_at` correspondan a
un comando que se ejecutó de verdad.

### 2.2 Segunda evaluación del menú — `NOT_OBSERVED`

Estado actual, sin cambiar en 25 h:

```
fingerprint   3fdd8a5b45db9cf3f085d6b80d440d24e5a159b126fd3384c55d9628372ce02a
published_at  2026-09-18 19:05:38
```

Publicaciones desde el despliegue: **1**, evidenciada por esa fila —
`mark_published` solo se llama tras una publicación confirmada.

Evaluaciones: **1**, *inferida* de que hay un solo `Connected to Telegram` en
todo el journal. Las evaluaciones no se registran en log, así que esto es
inferencia, no observación directa. Conviene decirlo.

**El criterio NO es `setMyCommands == 1`.** Es la coherencia entre el payload,
su huella y la decisión de publicar:

```
fingerprint igual    → published_at SIN cambiar  → sin republicar   PASA
fingerprint distinto → published_at actualizado  → republica        PASA
```

Un payload distinto **debe** republicar: si entre medias se añade un plugin,
una skill o cambia la prioridad del menú, el conjunto renderizado cambia y
publicar es lo correcto. Medir solo el contador leería un republish legítimo
como regresión.

Contar los `Connected to Telegram` da el número de evaluaciones. Con ≥2
evaluaciones y `published_at` intacto, el gate queda demostrado.

### 2.3 Los disparadores no se fabrican

El gateway no se reinicia solo: `Restart=always` solo actúa si el proceso
muere. Cuentan como reconexión legítima un reboot, un `hermes update`, un corte
de red que fuerce reconexión del adapter, o un reinicio que ya haga falta por
otro motivo.

Forzar un restart para cerrar el test convertiría una observación en una
puesta en escena. Si pasan días sin que ocurra ninguna, lo honesto es dejar la
condición como `NOT_OBSERVED` indefinido, no inventar el evento.

## 3. El backup vence el 21, y puede ser una prueba deliberada

**Aviso para una sesión futura: si `hermes-maint.service` aparece en rojo el
21 de septiembre, no es una regresión de Layer 2.**

### 3.1 No existe ningún ciclo automático de backup

`backup-hermes-home.sh` **solo corre cuando alguien lo lanza**. Nada lo
programa: el único cron del sistema es el de anclajes a las 07:00, y
`hermes-maint` **observa** backups, no los crea. Esperar a que «el ciclo
normal» renueve el backup es esperar algo que no existe.

### 3.2 La cuenta atrás

```
backup verificado más reciente   20260918-144610
                                 backup_verified=True  restore_verified=True
cumple 48 h                      2026-09-20 ~14:46

run del timer 2026-09-20 03:00   edad ~36 h   ->  Ok
run del timer 2026-09-21 03:00   edad ~60 h   ->  Degraded, exit 6
```

### 3.3 Las dos opciones, ambas defendibles

**Renovar antes del 20 a las 14:46.** Se lanza `backup-hermes-home.sh` y el
timer del 21 sigue verde. Mantiene protección fresca mientras se sigue
desarrollando.

**Dejarlo vencer.** El run del 21 sale `Degraded → exit 6` y
`hermes-maint.service` aparece en `systemctl --failed`. Sería **la primera vez
que la tarea se pone roja por un hallazgo real en producción**, y eso tiene
valor como prueba del monitor: confirmaría de una vez que un hallazgo se ve
como problema, que el timer sigue programado después de un fallo de su
service, y que el umbral de 48 h muerde cuando debe.

El coste es quedarse sin backup fresco mientras tanto. Es una decisión, no un
descuido — y por eso queda escrita antes de que pase.

En cualquiera de los dos casos, el `exit 6` del 21 sería **`backup-freshness`
funcionando**, nunca una regresión del despliegue de Layer 2.

## 4. Cuando ambos se cierren

La auditoría final es pequeña: las dos consultas de §2.1 y §2.2, más el conteo
de `Connected to Telegram`. Si las dos pasan, Layer 2 queda cerrado como
funcional **y** estable, y entonces tiene sentido preparar el merge de
`feat/layer2-schema` a `main` — antes de tocar Onion, no a la vez.
