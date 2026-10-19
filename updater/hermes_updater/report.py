"""Mensajes de Telegram. Texto plano, corto, y cada valor sale de una
comprobacion registrada: si algo no se observo, el mensaje lo dice."""

from __future__ import annotations

from typing import Optional

from .approval import approve_command

NAMES = {"hermes": "Hermes", "claude": "Claude", "codex": "Codex", "opencode": "OpenCode"}


def _v(pv: dict) -> str:
    return pv.get("installed") or "?"


def _line(product: str, pv: dict) -> str:
    n = NAMES[product]
    if pv.get("update_available") is True:
        return f"{n}: {_v(pv)} → {pv.get('available')} disponible"
    if pv.get("update_available") is False:
        return f"{n}: {_v(pv)} — Actualizado"
    return f"{n}: {_v(pv)} — sin datos ({(pv.get('detail') or pv.get('verdict') or '')[:80]})"


def daily_no_updates(versions: dict, *, gateway: str, backup: str, backup_detail: str = "",
                     maint: Optional[str] = None) -> str:
    lines = ["HERMES — Revisión diaria", ""]
    lines += [_line(p, versions[p]) for p in ("hermes", "claude", "codex", "opencode")]
    unknown = sum(1 for p in versions.values() if p.get("update_available") is None)
    lines += ["", f"Nuevas versiones: 0{f' (sin datos de {unknown})' if unknown else ''}",
              f"Gateway: {gateway}", f"Backup diario: {backup}"]
    if backup != "PASS" and backup_detail:
        lines.append(f"  motivo: {backup_detail[:300]}")
    if maint:
        lines.append(f"hermes-maint: {maint}")
    lines += ["", "No se requieren actualizaciones." if not unknown else "Revisión incompleta: faltan datos."]
    return "\n".join(lines)


def updates_available(versions: dict, aux: dict | None = None) -> str:
    lines = ["HERMES — Actualizaciones detectadas", ""]
    for p in ("hermes", "claude", "codex", "opencode"):
        pv = versions[p]
        if pv.get("update_available"):
            lines += [f"{NAMES[p]}:", f"Actual: {_v(pv)}", f"Disponible: {pv.get('available')}", ""]
        else:
            lines.append(_line(p, pv))
    if versions["hermes"].get("update_available"):
        lines += ["", "Preparando backup y validaciones..."]
    return "\n".join(lines).replace("\n\n\n", "\n\n")


def aux_results(results: dict) -> str:
    lines = ["HERMES — Herramientas auxiliares", ""]
    for p, r in results.items():
        lines.append(f"{NAMES[p]}: {r['verdict']} — {r.get('detail', '')[:160]}")
    return "\n".join(lines)


def backup_failed(phase: str, detail: str) -> str:
    return "\n".join(["HERMES — Backup fallido", "", f"Fase: {phase}", f"Motivo: {detail[:600]}", "",
                      "Bloqueada cualquier actualización de Hermes que dependa de este backup.",
                      "Producción no se ha tocado."])


def blocked(source: str, target: str, phase: str, detail: str, extra: list[str] = ()) -> str:
    lines = ["HERMES — Actualización bloqueada", "", f"Origen: {source}", f"Destino: {target}",
             f"Fase: {phase}", f"Motivo: {detail[:700]}"]
    lines += list(extra)
    lines += ["", "Producción no se ha tocado. Requiere intervención; no se reintenta el mismo bloqueo cada día."]
    return "\n".join(lines)


def prepared(*, source: str, target: str, checks: dict, code: str, expires: str, review_note: str = "",
             auto: bool = False) -> str:
    order = [("backup", "Backup restaurable"), ("sqlite", "Integridad SQLite"), ("review", "Revisión IA"),
             ("layer2", "Compatibilidad Layer 2"), ("tests", "Tests"), ("rollback", "Rollback preparado")]
    lines = ["HERMES — Actualización preparada", "", f"Origen: {source}", f"Destino: {target}", ""]
    lines += [f"{label}: {checks.get(k, 'NOT_OBSERVED')}" for k, label in order]
    if review_note:
        lines.append(f"  IA: {review_note[:300]}")
    if auto:
        lines += ["", "Se aplica automáticamente ahora (auto_apply).",
                  "Si algo falla, vuelve sola a la versión actual y te llega el motivo."]
        return "\n".join(lines)
    lines += ["", "Estado: PENDING_APPROVAL", "", "Se requiere autorización para producción.",
              f"Caduca: {expires}", "", "Para aprobar (pide el código TOTP):", approve_command(code)]
    return "\n".join(lines)


def final(*, versions_before: dict, versions_after: dict, checks: dict, aux: dict, errors: list,
          warnings: list, state: str) -> str:
    lines = ["HERMES — Reporte de actualización", "", "Hermes:",
             f"Versión anterior: {versions_before.get('hermes', '?')}",
             f"Versión nueva: {versions_after.get('hermes', '?')}", ""]
    for k in ("Backup", "Integración", "Tests", "Gateway", "Telegram", "Persistencia", "Rollback disponible"):
        lines.append(f"{k}: {checks.get(k, 'NOT_OBSERVED')}")
    lines.append("")
    for p in ("claude", "codex", "opencode"):
        lines.append(f"{NAMES[p]}: {aux.get(p, 'NOT_OBSERVED')}")
    lines += ["", f"Errores: {len(errors)}", f"Advertencias: {len(warnings)}"]
    for e in errors[:5]:
        lines.append(f"  ✗ {e[:200]}")
    for w in warnings[:5]:
        lines.append(f"  ! {w[:200]}")
    lines += ["", f"Estado final: {state}"]
    return "\n".join(lines)


def incident(*, phase: str, cause: str, actions: list, current: str, evidence: str) -> str:
    return "\n".join(["HERMES — INCIDENTE", "", f"Fase: {phase}", f"Causa: {cause[:500]}",
                      "Acciones realizadas:", *[f"  - {a[:200]}" for a in actions[:10]],
                      f"Estado actual: {current}", f"Evidencia: {evidence}", "",
                      "Se han detenido todas las acciones automáticas."])
