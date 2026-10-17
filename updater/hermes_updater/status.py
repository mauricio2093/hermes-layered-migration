"""Vocabulario cerrado de estados y veredictos.

Regla que este modulo hace cumplir: nada que no sea PASS cuenta como PASS.
UNKNOWN, NOT_OBSERVED, INSUFFICIENT_DATA o DEFERRED nunca se promocionan, lo
diga quien lo diga (la IA incluida).
"""

from __future__ import annotations

from enum import Enum


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_OBSERVED = "NOT_OBSERVED"
    DEFERRED = "DEFERRED"
    IN_PROGRESS = "IN_PROGRESS"
    SKIPPED = "SKIPPED"          # no ejecutado a proposito (y se dice por que)

    def __str__(self) -> str:    # para f-strings y json
        return self.value


#: Estados de la maquina. El orden es el del flujo; no todos se visitan.
class State(str, Enum):
    SCHEDULED = "SCHEDULED"
    PREFLIGHT = "PREFLIGHT"
    VERSION_DISCOVERY = "VERSION_DISCOVERY"
    UPDATE_CLASSIFICATION = "UPDATE_CLASSIFICATION"
    NO_UPDATES = "NO_UPDATES"
    DAILY_BACKUP = "DAILY_BACKUP"
    NOTIFY_CURRENT_VERSIONS = "NOTIFY_CURRENT_VERSIONS"
    UPDATES_AVAILABLE = "UPDATES_AVAILABLE"
    NOTIFY_AVAILABLE = "NOTIFY_AVAILABLE"
    AUX_UPDATES = "AUX_UPDATES"
    BACKUP = "BACKUP"
    BACKUP_VERIFICATION = "BACKUP_VERIFICATION"
    STAGING_PREPARATION = "STAGING_PREPARATION"
    COMPATIBILITY_TESTS = "COMPATIBILITY_TESTS"
    AI_REVIEW = "AI_REVIEW"
    UPDATE_READY = "UPDATE_READY"
    AUTHORIZATION_GATE = "AUTHORIZATION_GATE"
    CONTROLLED_DEPLOYMENT = "CONTROLLED_DEPLOYMENT"
    SERVICE_RECOVERY = "SERVICE_RECOVERY"
    POST_UPDATE_TESTS = "POST_UPDATE_TESTS"
    ROLLBACK = "ROLLBACK"
    VERIFY_RECOVERY = "VERIFY_RECOVERY"
    TELEGRAM_REPORT = "TELEGRAM_REPORT"
    COMPLETE = "COMPLETE"

    def __str__(self) -> str:
        return self.value


#: Estados en los que produccion puede estar a medio cambiar. Una ejecucion
#: interrumpida en uno de ellos NO se da por cerrada: se reconcilia (rollback
#: al punto registrado y verificacion) antes de hacer nada mas.
DEPLOY_STATES = frozenset({State.CONTROLLED_DEPLOYMENT, State.SERVICE_RECOVERY,
                           State.POST_UPDATE_TESTS, State.ROLLBACK, State.VERIFY_RECOVERY})


class Outcome(str, Enum):
    """Resultado de una ejecucion entera. Solo SUCCESS y NO_UPDATES son verdes."""
    SUCCESS = "SUCCESS"                    # todo lo obligatorio en PASS
    NO_UPDATES = "NO_UPDATES"              # revision completa, nada que actualizar
    PENDING_APPROVAL = "PENDING_APPROVAL"  # candidato listo, falta autorizacion
    BLOCKED = "BLOCKED"                    # una precondicion impide seguir (sin cambios)
    FAILED = "FAILED"                      # algo fallo; produccion no se toco
    ROLLED_BACK = "ROLLED_BACK"            # se desplego, fallo y se recupero
    RECOVERY_FAILED = "RECOVERY_FAILED"    # INCIDENTE: el rollback no dejo el servicio sano
    INTERRUPTED = "INTERRUPTED"            # la ejecucion murio; reconciliada despues

    def __str__(self) -> str:
        return self.value


GREEN = frozenset({Outcome.SUCCESS, Outcome.NO_UPDATES})


def is_pass(v) -> bool:
    return v == Verdict.PASS or v == "PASS"


def worst(verdicts) -> Verdict:
    """El peor de varios veredictos. Vacio -> INSUFFICIENT_DATA, nunca PASS."""
    order = [Verdict.FAIL, Verdict.BLOCKED, Verdict.INSUFFICIENT_DATA, Verdict.NOT_OBSERVED,
             Verdict.DEFERRED, Verdict.IN_PROGRESS, Verdict.SKIPPED, Verdict.PASS]
    vs = [Verdict(v) for v in verdicts]
    if not vs:
        return Verdict.INSUFFICIENT_DATA
    return min(vs, key=order.index)
