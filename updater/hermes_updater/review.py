"""Auditoria con IA: un auditor especializado, NUNCA una autoridad.

LEARNING != AUTHORITY:
  * La IA recibe un paquete de EVIDENCIAS ya redactado (sin secretos, sin
    bases de datos, sin .env, sin rutas personales) en un directorio vacio,
    con sandbox de solo lectura y sin red de herramientas.
  * Devuelve un JSON con un esquema cerrado. Su veredicto solo puede
    EMPEORAR el resultado determinista: ``concerns`` o ``block`` bajan el
    candidato; ``pass`` no sube nada que no estuviera ya en PASS.
  * No puede autorizar sudo/root, SSH/firewall, credenciales, TOTP, comandos
    privilegiados, permisos, proveedores ni politicas: esas palabras en su
    respuesta no tienen ningun efecto, y si "recomienda" algo de eso se marca.
  * Si no esta disponible: DEFERRED (o BLOCKED si la politica la exige). No se
    inventa aprobacion. UNKNOWN / NOT_OBSERVED / INSUFFICIENT_DATA nunca pasan
    a PASS por su opinion.

Costes: con suscripcion (Codex/ChatGPT, Claude) el coste variable real es 0 y
asi se registra; el equivalente API solo se calcula si hay tokens observados.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .proc import Runner
from .status import Verdict

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "risks", "affected_customizations", "recommendations", "evidence_gaps"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "concerns", "block"]},
        "summary": {"type": "string"},
        "risks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["severity", "area", "description", "evidence"],
            "properties": {"severity": {"type": "string", "enum": ["low", "medium", "high"]},
                           "area": {"type": "string"}, "description": {"type": "string"},
                           "evidence": {"type": "string"}}}},
        "affected_customizations": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "evidence_gaps": {"type": "array", "items": {"type": "string"}},
    },
}

INSTRUCTIONS = """Eres un auditor de compatibilidad para una actualizacion de Hermes Agent.
La evidencia va al final de este mensaje (evidence.json). No ejecutes comandos,
no leas otros ficheros, no accedas a la red, no modifiques nada.

Tu papel es asesor: no autorizas nada. Las decisiones de despliegue, privilegios,
credenciales, TOTP, SSH, firewall y politicas las toman humanos y comprobaciones
deterministas. No pidas ni propongas cambiarlas.

Evalua:
1. El backup verificado (checks independientes).
2. Los cambios upstream y su solapamiento con los commits propios (Capa 2).
3. Personalizaciones afectadas (Telegram, plugins, hermes-control, layer2).
4. Migraciones de esquema SQLite y de config.
5. Riesgos de compatibilidad.
6. Resultados de pruebas: un fallo NO es aceptable salvo que este demostrado
   como preexistente en la linea base.
7. Huecos de evidencia: lo que no se pudo observar NO cuenta como correcto.

Responde con JSON segun el esquema. verdict:
  pass      sin riesgos relevantes con la evidencia dada
  concerns  riesgos que un humano debe leer antes de aprobar
  block     evidencia de que el candidato romperia algo
"""

FORBIDDEN_TOPICS = re.compile(r"\b(sudo|root|ssh|firewall|ufw|totp|chmod|chown|credencial|credential|"
                              r"token|password|contrase|nopasswd|sudoers)\b", re.I)

_SECRET_PATTERNS = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),            # bot token de Telegram
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|authorization)\b\s*[:=]\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[\w.+-]+@(?!users\.noreply\.github\.com)[\w-]+\.[\w.-]+\b"),
]


def redact(text: str, home: str) -> str:
    for p in _SECRET_PATTERNS:
        text = p.sub("[REDACTADO]", text)
    if home:
        text = text.replace(home, "~")
    return text


def build_evidence(*, versions: dict, backup: dict, candidate: dict, tests: list, git_log: str,
                   home: str, max_bytes: int = 120_000) -> dict:
    """Solo resumenes. Nunca: .env, auth.json, config.yaml, bases, salidas crudas."""
    ev = {
        "versions": {k: {kk: v.get(kk) for kk in ("installed", "available", "update_available", "verdict", "method")}
                     for k, v in versions.items()},
        "backup": {"verdict": backup.get("verdict"), "backup_id": backup.get("backup_id"),
                   "independent_checks": {k: {"verdict": c.get("verdict"), "detail": c.get("detail")}
                                          for k, c in ((backup.get("verification") or {}).get("checks") or {}).items()},
                   "preupdate_parts": {k: (v.get("verdict") if isinstance(v, dict) and "verdict" in v else None)
                                       for k, v in (backup.get("parts") or {}).items()}},
        "candidate": {k: candidate.get(k) for k in ("verdict", "source_commit", "target_tag", "target_commit",
                                                     "candidate_commit", "conflicts", "detail")},
        "analysis": {k: (candidate.get("analysis") or {}).get(k) for k in (
            "ref_integrity", "carried", "carried_still_needed", "carried_already_upstream", "merge_base",
            "upstream_commits", "upstream_files_changed", "overlap_with_carried", "plugin_api_files_changed",
            "migration_files_changed", "schema_version", "config_version", "dependencies",
            "target_contains_source_upstream_base", "recorded_resolutions", "recorded_resolution_diff")},
        "dependencies": {k: (candidate.get("deps") or {}).get(k)
                         for k in ("extras", "extras_gone_in_target", "pip_check", "changed", "dropped")},
        "tests": [{k: t.get(k) for k in ("name", "required", "verdict", "passed", "failed", "errors", "skipped",
                                         "regressions", "preexisting", "flaky", "detail")} for t in tests],
        "upstream_commits_touching_customized_files": git_log[-40_000:],
    }
    raw = redact(json.dumps(ev, indent=1, default=str), home)
    if len(raw.encode()) > max_bytes:
        ev["upstream_commits_touching_customized_files"] = "[recortado por tamano]"
        raw = redact(json.dumps(ev, indent=1, default=str), home)
    return json.loads(raw)


@dataclass
class Review:
    verdict: Verdict = Verdict.DEFERRED
    ai_verdict: Optional[str] = None
    provider: Optional[str] = None
    summary: str = ""
    risks: list = field(default_factory=list)
    recommendations: list = field(default_factory=list)
    evidence_gaps: list = field(default_factory=list)
    affected: list = field(default_factory=list)
    flagged: list = field(default_factory=list)   # recomendaciones fuera de su mandato
    cost: dict = field(default_factory=dict)
    detail: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["verdict"] = self.verdict.value
        return d


def _provider_argv(provider: str, workdir: Path, schema_path: Path, out_path: Path) -> Optional[list]:
    if provider == "codex":
        # Menos acotado que claude: el sandbox read-only impide escribir y la
        # red, pero NO impide leer el sistema de ficheros. Solo como alternativa.
        return ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
                "--color", "never", "-C", str(workdir), "--output-schema", str(schema_path),
                "-o", str(out_path), "-"]
    if provider == "claude":
        # --tools "": SIN herramientas. La evidencia va en el prompt; no puede
        # leer el disco, ni ejecutar nada, ni usar MCP.
        return ["claude", "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
                "--disable-slash-commands", "--model", "sonnet", "--output-format", "json",
                "--json-schema", json.dumps(SCHEMA)]
    return None


def interpret(raw: dict, *, deterministic: Verdict) -> Review:
    """Convierte la respuesta en un veredicto que SOLO puede empeorar el
    determinista. ``deterministic`` es lo que dicen backup + tests + merge."""
    rv = Review(ai_verdict=raw.get("verdict"), summary=str(raw.get("summary", ""))[:2000],
                risks=list(raw.get("risks") or [])[:30], recommendations=list(raw.get("recommendations") or [])[:20],
                evidence_gaps=list(raw.get("evidence_gaps") or [])[:20],
                affected=list(raw.get("affected_customizations") or [])[:30])
    rv.flagged = [r for r in rv.recommendations if FORBIDDEN_TOPICS.search(str(r))]
    ai = rv.ai_verdict
    if ai not in ("pass", "concerns", "block"):
        rv.verdict, rv.detail = Verdict.DEFERRED, f"respuesta fuera de esquema ({ai!r})"
        return rv
    if ai == "block":
        rv.verdict = Verdict.BLOCKED
    elif ai == "concerns" or any(r.get("severity") == "high" for r in rv.risks if isinstance(r, dict)):
        rv.verdict = Verdict.DEFERRED     # un humano debe leerlo; no es PASS
    else:
        rv.verdict = Verdict.PASS
    if deterministic != Verdict.PASS and rv.verdict == Verdict.PASS:
        # la IA no promociona nada: lo determinista manda
        rv.verdict = deterministic
        rv.detail = "la IA dijo pass, pero las comprobaciones deterministas no estan en PASS"
    return rv


def run_review(cfg: Config, runner: Runner, evidence: dict, *, deterministic: Verdict,
               provider: Optional[str] = None, tmp_root: Optional[Path] = None) -> Review:
    provider = provider or cfg.policy.ai_provider
    unavailable = Verdict.BLOCKED if cfg.policy.ai_required else Verdict.DEFERRED
    if provider == "none":
        return Review(verdict=unavailable, provider="none", detail="IA desactivada por politica")
    if shutil.which(provider) is None:
        return Review(verdict=unavailable, provider=provider, detail=f"{provider} no instalado")
    root = Path(tmp_root or cfg.work_dir)
    root.mkdir(parents=True, exist_ok=True)
    wd = Path(tempfile.mkdtemp(prefix="ai-review-", dir=root))
    try:
        (wd / "evidence.json").write_text(json.dumps(evidence, indent=1))
        schema = wd / "schema.json"
        schema.write_text(json.dumps(SCHEMA))
        out = wd / "answer.json"
        argv = _provider_argv(provider, wd, schema, out)
        prompt = INSTRUCTIONS + "\n\nEVIDENCIA (evidence.json):\n" + json.dumps(evidence)[:150_000]
        r = runner.run(argv, cwd=wd, timeout=cfg.policy.ai_timeout_s, stdin=prompt,
                       env={"NO_COLOR": "1"})
        if not r.ok:
            return Review(verdict=unavailable, provider=provider,
                          detail=f"IA no disponible (rc={r.code}{', timeout' if r.timed_out else ''}): {r.tail(300)}",
                          cost={"billing": "subscription", "variable_cost_usd": 0.0, "api_equivalent_usd": None})
        try:
            if provider == "codex":
                raw = json.loads(out.read_text())
            else:
                env = json.loads(r.out)
                raw = env.get("structured_output") or json.loads(env.get("result", "{}"))
        except (OSError, ValueError, AttributeError) as exc:
            return Review(verdict=unavailable, provider=provider, detail=f"respuesta ilegible: {exc}")
        rv = interpret(raw, deterministic=deterministic)
        rv.provider = provider
        rv.cost = {"billing": "subscription", "variable_cost_usd": 0.0,
                   "api_equivalent_usd": None, "note": "suscripcion: no es gasto API; tokens no observados"}
        return rv
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def git_log_for(runner: Runner, repo: Path, rng: str, paths: list, limit: int = 300) -> str:
    if not paths:
        return ""
    r = runner.run(["git", "-C", str(repo), "log", "--no-merges", f"-{limit}", "--format=%h %s", rng, "--", *paths],
                   timeout=300, keep=None)
    return r.out if r.ok else ""
