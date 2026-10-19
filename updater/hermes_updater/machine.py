"""La maquina de estados. Une los modulos; no decide nada que no este en una
comprobacion registrada.

Dos entradas:
  daily()  SCHEDULED -> PREFLIGHT -> VERSION_DISCOVERY -> UPDATE_CLASSIFICATION
           -> (NO_UPDATES -> DAILY_BACKUP -> NOTIFY_CURRENT_VERSIONS)
            | (UPDATES_AVAILABLE -> NOTIFY_AVAILABLE -> AUX_UPDATES
               -> [Hermes] BACKUP -> BACKUP_VERIFICATION -> STAGING_PREPARATION
                  -> COMPATIBILITY_TESTS -> AI_REVIEW -> UPDATE_READY -> AUTHORIZATION_GATE)
           -> TELEGRAM_REPORT -> COMPLETE
  apply()  AUTHORIZATION_GATE -> CONTROLLED_DEPLOYMENT -> SERVICE_RECOVERY
           -> POST_UPDATE_TESTS -> (COMPLETE | ROLLBACK -> VERIFY_RECOVERY)
           -> TELEGRAM_REPORT

Nota de orden: AI_REVIEW va DESPUES de las pruebas (no antes, como en el
esquema inicial): el auditor tiene que poder evaluar sus resultados.

daily() nunca modifica produccion. Solo apply() lo hace, y solo con una
aprobacion escrita por root tras verificar TOTP.
"""

from __future__ import annotations

import json
import shutil
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import approval, auxiliary, backup, ci, deploy, notify, report, review, staging, verify, versions
from .config import Config, ensure_dirs
from .proc import Runner
from .status import DEPLOY_STATES, GREEN, Outcome, State, Verdict
from .store import Busy, Store, exclusive


@dataclass
class Ops:
    """Cada fase es inyectable: los tests de fallo sustituyen una sola."""
    discover: Callable = versions.discover
    backup_daily: Callable = backup.run_backup_a
    backup_preupdate: Callable = backup.run_preupdate
    prepare: Callable = staging.prepare
    run_tests: Callable = ci.run_all
    review: Callable = review.run_review
    aux_update: Callable = auxiliary.update_one
    deploy: Callable = deploy.deploy


class Orchestrator:
    def __init__(self, cfg: Config, *, runner: Optional[Runner] = None, svc=None,
                 transport: Optional[notify.Transport] = None, ops: Optional[Ops] = None,
                 discover_kwargs: Optional[dict] = None, clock=time.time, sleep=time.sleep,
                 enable_aux: bool = True, enable_prepare: bool = True):
        self.cfg = cfg
        self.runner = runner or Runner()
        self.svc = svc or deploy.SystemdUser(self.runner, cfg.gateway_unit)
        self.transport = transport or notify.transport_for(cfg, self.runner)
        self.ops = ops or Ops()
        self.discover_kwargs = discover_kwargs or {}
        self.clock, self.sleep = clock, sleep
        self.enable_aux, self.enable_prepare = enable_aux, enable_prepare
        self.daily_result: Optional[backup.BackupResult] = None

    # --- envoltorio comun ----------------------------------------------------------
    def _guarded(self, kind: str, trigger: str, body: Callable[[Store, int], Outcome]) -> tuple[Optional[int], Outcome]:
        ensure_dirs(self.cfg)
        try:
            with exclusive(self.cfg.lock_path):
                store = Store(self.cfg.db_path, clock=self.clock)
                try:
                    self.reconcile(store)
                    rid = store.open_run(kind, trigger)
                    outcome = Outcome.FAILED
                    try:
                        outcome = body(store, rid)
                    except Exception as exc:              # noqa: BLE001 - se registra y se reporta
                        tb = traceback.format_exc()
                        store.check(rid, "orchestrator", "exception", Verdict.FAIL,
                                    detail=f"{type(exc).__name__}: {exc}", evidence=tb[-4000:])
                        notify.enqueue(store, rid, f"exception:{rid}", "incident", report.incident(
                            phase=store.run(rid)["state"], cause=f"{type(exc).__name__}: {exc}",
                            actions=["ejecucion abortada"], current="ver `hermes-updater status`",
                            evidence=f"run {rid}"))
                        outcome = Outcome.FAILED
                    finally:
                        try:
                            store.close_run(rid, outcome)
                        except RuntimeError as exc:
                            # nunca un falso verde: si no puede cerrarse como verde, FAILED
                            store.check(rid, "orchestrator", "close", Verdict.FAIL, detail=str(exc))
                            outcome = Outcome.FAILED
                            store.close_run(rid, outcome)
                        res = notify.flush(store, self.transport, retries=self.cfg.policy.notify_retries,
                                           sleep=self.sleep)
                        store.merge_data(rid, notify=res)
                    return rid, outcome
                finally:
                    store.close()
        except Busy:
            return None, Outcome.BLOCKED

    def _to(self, store: Store, rid: int, st: State, note: str = "") -> None:
        store.transition(rid, st, note)

    # --- reconciliacion -------------------------------------------------------------
    def reconcile(self, store: Store) -> list:
        """Ejecuciones abiertas cuyo proceso murio. Las de despliegue se
        RECUPERAN (post-pruebas o rollback); las demas se cierran INTERRUPTED."""
        done = []
        for row in store.open_runs():
            data = json.loads(row["data"])
            st = State(row["state"])
            store.check(row["id"], "orchestrator", "interrupted", Verdict.FAIL,
                        detail=f"proceso {row['pid']} murio en {st.value}")
            if st in DEPLOY_STATES and data.get("rollback_point", {}).get("old_head"):
                rp = data["rollback_point"]
                phase = data.get("phase")
                if phase == "rollback_point":
                    # aun no se habia tocado el codigo: solo limpiar la copia del venv
                    shutil.rmtree(rp.get("venv_backup", "/nonexistent"), ignore_errors=True)
                    result = "sin cambios en produccion"
                else:
                    payload = data.get("payload") or {}
                    post = deploy.post_tests(self.cfg, self.runner, payload, self.svc, rp) if payload else []
                    if post and all(s.verdict == Verdict.PASS for s in post):
                        result = "el despliegue habia terminado bien: post-pruebas PASS"
                    else:
                        res = deploy.DeployResult(rollback_point=rp)
                        ok = deploy.rollback(self.cfg, self.runner, self.svc, rp, res)
                        healthy, why = deploy.wait_healthy(self.svc, timeout_s=self.cfg.policy.service_ready_timeout_s,
                                                           stable_s=self.cfg.policy.service_stable_s, sleep=self.sleep)
                        result = f"rollback {'OK' if ok and healthy else 'FALLIDO'}: {why}"
                        if not (ok and healthy):
                            notify.enqueue(store, row["id"], f"incident:reconcile:{row['id']}", "incident",
                                           report.incident(phase=st.value, cause="despliegue interrumpido y rollback fallido",
                                                           actions=[s.detail for s in res.steps], current="gateway NO sano",
                                                           evidence=f"run {row['id']}"))
                store.merge_data(row["id"], reconcile=result)
            store.close_run(row["id"], Outcome.INTERRUPTED)
            done.append(row["id"])
        return done

    # --- diario -------------------------------------------------------------------
    def daily(self, trigger: str = "timer") -> tuple[Optional[int], Outcome]:
        return self._guarded("daily", trigger, self._daily)

    def _daily(self, store: Store, rid: int) -> Outcome:
        cfg, pol = self.cfg, self.cfg.policy
        self._to(store, rid, State.PREFLIGHT)
        free_gb = shutil.disk_usage(cfg.state_dir).free / 2**30
        mem = _mem_available_mb()
        res_ok = free_gb >= pol.min_free_disk_gb and (mem is None or mem >= pol.min_avail_mem_mb)
        store.check(rid, "preflight", "resources", Verdict.PASS if res_ok else Verdict.BLOCKED,
                    detail=f"disco {free_gb:.1f} GiB, memoria disponible {mem} MB")
        gw = self.svc.is_active()
        store.check(rid, "preflight", "gateway", Verdict.PASS if gw else Verdict.FAIL, required=False,
                    detail="hermes-gateway activo" if gw else "hermes-gateway NO activo")
        maint = _maint_last(cfg)
        store.check(rid, "preflight", "hermes_maint", Verdict.PASS if maint.get("outcome") == "ok" else Verdict.NOT_OBSERVED,
                    required=False, detail=f"ultima observacion: {maint.get('outcome')} {maint.get('detail', '')}"[:300])

        # Politica A: el backup diario va ANTES de mirar versiones. No depende de
        # que haya red, de que haya actualizaciones ni de que la deteccion no
        # falle: una excepcion en una fase posterior ya no puede impedirlo.
        b = self._daily_backup(store, rid, res_ok)
        self.daily_result = b

        self._to(store, rid, State.VERSION_DISCOVERY)
        vers = self.ops.discover(cfg, self.runner, **self.discover_kwargs)
        vd = {k: v.to_dict() for k, v in vers.items()}
        store.merge_data(rid, versions=vd)
        for k, v in vers.items():
            store.check(rid, "versions", k, v.verdict, detail=f"{v.installed} -> {v.available} {v.detail}"[:300])

        self._to(store, rid, State.UPDATE_CLASSIFICATION)
        aux_up = [p for p in ("claude", "codex", "opencode") if vers[p].update_available]
        h = vers["hermes"]
        hermes_up = bool(h.update_available)
        skip_reason = None
        if hermes_up:
            skip_reason = self._hermes_skip_reason(store, h)
        store.merge_data(rid, classification={"aux": aux_up, "hermes": hermes_up, "hermes_skip": skip_reason})

        if not aux_up and not hermes_up:
            self._to(store, rid, State.NO_UPDATES)
            self._to(store, rid, State.NOTIFY_CURRENT_VERSIONS)
            day = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
            notify.enqueue(store, rid, f"daily:{day}", "daily", report.daily_no_updates(
                vd, gateway="PASS" if gw else "FAIL", backup=b.verdict.value, backup_detail=b.detail,
                maint=maint.get("outcome")))
            self._to(store, rid, State.COMPLETE)
            if any(v.verdict != Verdict.PASS for v in vers.values()):
                return Outcome.BLOCKED if any(v.extra.get("network_error") for v in vers.values()) else Outcome.FAILED
            return Outcome.NO_UPDATES if b.verdict == Verdict.PASS else Outcome.FAILED

        self._to(store, rid, State.UPDATES_AVAILABLE, f"aux={aux_up} hermes={hermes_up}")
        self._to(store, rid, State.NOTIFY_AVAILABLE)
        key = "available:" + ",".join(f"{p}={vd[p].get('available')}" for p in ["hermes", *aux_up]
                                      if vd[p].get("update_available"))
        notify.enqueue(store, rid, key, "available", report.updates_available(vd))

        aux_results = {}
        if aux_up and self.enable_aux and res_ok:
            self._to(store, rid, State.AUX_UPDATES)
            for p in aux_up:
                r = self.ops.aux_update(cfg, self.runner, vers[p],
                                        version_of=lambda p=p: _requery(cfg, self.runner, p, **self.discover_kwargs))
                aux_results[p] = r.to_dict()
                store.check(rid, f"aux:{p}", "update", r.verdict, required=False, detail=r.detail)
            store.merge_data(rid, aux=aux_results)
            notify.enqueue(store, rid, f"aux:{rid}", "aux", report.aux_results(aux_results))
        elif aux_up:
            for p in aux_up:
                store.check(rid, f"aux:{p}", "update", Verdict.SKIPPED, required=False,
                            detail="recursos insuficientes" if not res_ok else "desactivado en esta ejecucion")

        outcome = Outcome.SUCCESS
        if hermes_up and skip_reason is None and self.enable_prepare and res_ok:
            outcome = self._prepare_hermes(store, rid, vers, vd)
        else:
            if hermes_up:
                store.check(rid, "hermes", "preparation", Verdict.SKIPPED, required=False,
                            detail=skip_reason or ("recursos insuficientes" if not res_ok else "desactivado en esta ejecucion"))
        if b.verdict != Verdict.PASS and outcome in (Outcome.SUCCESS, Outcome.PENDING_APPROVAL):
            outcome = Outcome.FAILED
        self._to(store, rid, State.TELEGRAM_REPORT)
        self._to(store, rid, State.COMPLETE)
        if outcome == Outcome.SUCCESS and any(v.verdict != Verdict.PASS for v in vers.values()):
            outcome = Outcome.FAILED
        return outcome

    def _hermes_skip_reason(self, store: Store, h) -> Optional[str]:
        target = h.available
        source = h.extra["installed"]["commit"]
        pend = store.pending_candidate()
        if pend is not None:
            p = json.loads(pend["payload"])
            if p["target"]["tag"] == target and p["source"]["commit"] == source and pend["expires_at"] > self.clock():
                return f"candidato {pend['id']} ya preparado y pendiente de aprobacion"
        last = store.last_attempt(target, source)
        # La espera es para fallos DEL CANDIDATO (merge, pruebas, revision). Un
        # backup fallido no dice nada del objetivo: cada ejecucion repite el
        # backup y sin el no se prepara nada, asi que no hay bucle que frenar.
        # (Las filas antiguas lo registraban como FAILED "backup: ...".)
        if last is not None and last["outcome"] in ("BLOCKED", "FAILED") \
                and not (last["reason"] or "").startswith("backup:"):
            age_h = (self.clock() - last["at"]) / 3600
            if age_h < self.cfg.policy.retry_blocked_after_h:
                return (f"el mismo objetivo ({target} sobre {source[:10]}) termino {last['outcome']} hace "
                        f"{age_h:.0f} h: {last['reason'][:160]}; se reintenta tras {self.cfg.policy.retry_blocked_after_h} h")
        return None

    def _daily_backup(self, store: Store, rid: int, res_ok: bool) -> backup.BackupResult:
        self._to(store, rid, State.DAILY_BACKUP)
        if not self.cfg.policy.daily_backup:
            b = backup.BackupResult("daily", verdict=Verdict.SKIPPED, detail="desactivado por politica")
        elif not res_ok:
            b = backup.BackupResult("daily", verdict=Verdict.BLOCKED, detail="recursos insuficientes")
        else:
            try:
                b = self.ops.backup_daily(self.cfg, self.runner)
            except Exception as exc:                          # noqa: BLE001 - se registra; la revision sigue
                b = backup.BackupResult("daily", verdict=Verdict.FAIL,
                                        detail=f"el backup lanzo {type(exc).__name__}: {exc}")
        store.check(rid, "backup", "daily", b.verdict, detail=b.detail, evidence=b.evidence_path)
        store.merge_data(rid, daily_backup=b.to_dict())
        if b.verdict == Verdict.PASS and b.backup_dir:
            self._own_backup(store, rid, Path(b.backup_dir).name)
            removed = backup.prune(self.cfg.daily_backups, self._owned(store), self.cfg.policy.keep_daily_backups)
            if removed:
                store.merge_data(rid, pruned=removed)
        elif b.backup_dir:
            self._own_backup(store, rid, Path(b.backup_dir).name)
            day = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
            notify.enqueue(store, rid, f"backup-failed:{day}", "backup_failed", report.backup_failed("backup diario", b.detail))
        return b

    def _own_backup(self, store: Store, rid: int, name: str) -> None:
        store.db.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (f"owned_backup:{name}", str(rid)))

    def _owned(self, store: Store) -> set:
        return {r["key"].split(":", 1)[1] for r in store.db.execute("SELECT key FROM meta WHERE key LIKE 'owned_backup:%'")}

    # --- preparacion de Hermes ------------------------------------------------------
    def _prepare_hermes(self, store: Store, rid: int, vers: dict, vd: dict) -> Outcome:
        cfg = self.cfg
        h = vers["hermes"]
        inst = h.extra["installed"]
        source, target = inst["commit"], h.available
        tgt = h.extra["target"]
        src_label = h.extra.get("display") or h.installed

        def block(phase: str, detail: str, extra=(), outcome=Outcome.BLOCKED) -> Outcome:
            store.record_attempt(target, source, outcome.value, f"{phase}: {detail}")
            notify.enqueue(store, rid, f"blocked:{target}:{source}:{phase}", "blocked",
                           report.blocked(src_label, target, phase, detail, list(extra)))
            return outcome

        if inst.get("dirty_tracked"):
            store.check(rid, "hermes", "clean_tree", Verdict.BLOCKED, detail="checkout productivo con cambios sin commit")
            return block("PREFLIGHT", "el checkout productivo tiene cambios sin commit")

        self._to(store, rid, State.BACKUP)
        # El A de ESTA ejecucion (minutos de antiguedad, verificado) es el del
        # pre-update: nunca uno de otro dia. Si fallo, el pre-update falla.
        b = self.ops.backup_preupdate(cfg, self.runner, versions=vd, reuse_a=self.daily_result,
                                      upstream_ref=_upstream_base(cfg, self.runner, source, tgt["commit"]))
        store.merge_data(rid, preupdate_backup=b.to_dict())
        self._to(store, rid, State.BACKUP_VERIFICATION)
        store.check(rid, "backup", "pre_update", b.verdict, detail=b.detail, evidence=b.evidence_path)
        vchecks = (b.verification or {}).get("checks", {})
        sqlite_ok = all(vchecks.get(k, {}).get("verdict") == "PASS" for k in ("db_inventory", "restore_databases"))
        if b.verdict != Verdict.PASS:
            notify.enqueue(store, rid, f"backup-failed:pre:{target}:{source}", "backup_failed",
                           report.backup_failed("backup previo a actualizacion", b.detail))
            store.record_attempt(target, source, "BACKUP_FAILED", "backup: " + b.detail)
            return Outcome.FAILED

        self._to(store, rid, State.STAGING_PREPARATION)
        self._clean_staging(store)
        cand = self.ops.prepare(cfg, self.runner, target_tag=target, target_commit=tgt["commit"],
                                tag_object=tgt.get("tag_object"))
        cd = cand.to_dict()
        store.merge_data(rid, candidate=cd)
        store.check(rid, "staging", "candidate", cand.verdict, detail=cand.detail)
        if cand.verdict != Verdict.PASS:
            extra = []
            if cand.conflicts:
                extra.append("Conflictos: " + ", ".join(cand.conflicts))
            a = cand.analysis or {}
            if a.get("carried_still_needed") is not None:
                extra.append(f"Commits propios a conservar: {a['carried_still_needed']}; ya en upstream: "
                             f"{len(a.get('carried_already_upstream') or [])}")
            if a.get("overlap_with_carried"):
                extra.append("Ficheros propios tocados por upstream: " + ", ".join(a["overlap_with_carried"][:8]))
            return block("STAGING_PREPARATION", cand.detail, extra,
                         Outcome.BLOCKED if cand.verdict == Verdict.BLOCKED else Outcome.FAILED)

        self._to(store, rid, State.COMPATIBILITY_TESTS)
        rep = verify.verify_backup(Path(b.backup_dir), hermes_home=cfg.hermes_home, work_dir=cfg.work_dir,
                                   keep_restore=True, prod_repo=cfg.prod_repo, gateway_unit=cfg.gateway_unit)
        restored = Path(rep.restored_to) / cfg.hermes_home.name if rep.restored_to else None
        try:
            carried_tests = sorted({f for c in cand.analysis.get("carried", []) for f in c["files"]
                                    if f.startswith("tests/") and f.endswith(".py")})
            suites = self.ops.run_tests(cfg, self.runner, python=str(Path(cand.venv) / "bin" / "python"),
                                        cand_repo=Path(cand.repo), target_tag=target, carried_tests=carried_tests,
                                        restored_home=restored, out=Path(cand.workdir) / "ci")
        finally:
            if rep.restored_to:
                shutil.rmtree(rep.restored_to, ignore_errors=True)
        sd = [s.to_dict() for s in suites]
        store.merge_data(rid, tests=sd)
        for s in suites:
            store.check(rid, "tests", s.name, s.verdict, required=s.required, detail=s.detail)
        tests_v = ci.overall(suites)
        if tests_v != Verdict.PASS:
            bad = [f"{s.name}: {s.verdict.value} {s.detail[:120]}" for s in suites if s.verdict != Verdict.PASS]
            return block("COMPATIBILITY_TESTS", "pruebas sin PASS", bad, Outcome.FAILED)

        self._to(store, rid, State.AI_REVIEW)
        git_log = review.git_log_for(self.runner, Path(cand.repo), f"{cand.analysis.get('merge_base')}..{tgt['commit']}",
                                     cand.analysis.get("overlap_with_carried") or [])
        evidence = review.build_evidence(versions=vd, backup=b.to_dict(), candidate=cd, tests=sd, git_log=git_log,
                                         home=str(Path.home()))
        deterministic = Verdict.PASS          # backup, staging y tests ya estan en PASS aqui
        rv = self.ops.review(cfg, self.runner, evidence, deterministic=deterministic)
        store.merge_data(rid, review=rv.to_dict())
        # La IA no es obligatoria para el PASS de la ejecucion; su veredicto viaja al humano.
        store.check(rid, "review", "ai", rv.verdict, required=cfg.policy.ai_required,
                    detail=(rv.summary or rv.detail)[:500])
        if rv.verdict == Verdict.BLOCKED:
            return block("AI_REVIEW", "el auditor IA encontro evidencia de rotura: " + (rv.summary or rv.detail)[:300],
                         [f"riesgo {r.get('severity')}: {r.get('description', '')[:150]}" for r in rv.risks[:5]])

        self._to(store, rid, State.UPDATE_READY)
        lock = Path(cand.deps["lock"])
        payload = approval.make_payload(
            source_commit=source, source_version=src_label, target_tag=target, target_commit=tgt["commit"],
            candidate_commit=cand.candidate_commit, candidate_branch=cand.branch, staging_repo=cand.repo,
            staging_venv=cand.venv, lock_sha256=verify.sha256_file(lock), backup_id=b.backup_id,
            preupdate_dir=b.bundle_dir, preupdate_manifest=verify.sha256_file(Path(b.bundle_dir) / "MANIFEST.sha256"),
            tests={s.name: s.verdict.value for s in suites}, review=rv.verdict.value,
            created_at=self.clock(), ttl_s=cfg.policy.candidate_ttl_h * 3600)
        code = approval.candidate_code(payload)
        store.save_candidate(code, rid, payload, cfg.policy.candidate_ttl_h * 3600)
        store.merge_data(rid, candidate_id=code)
        self._to(store, rid, State.AUTHORIZATION_GATE, f"candidato {code}")
        layer2 = Verdict.PASS if all(s.verdict == Verdict.PASS for s in suites if s.name in ("upstream-layer2", "hermes-control")) else Verdict.FAIL
        notify.enqueue(store, rid, f"prepared:{code}", "prepared", report.prepared(
            source=src_label, target=target, code=code,
            expires=time.strftime("%Y-%m-%d %H:%M", time.localtime(payload["expires_at"])),
            checks={"backup": b.verdict.value, "sqlite": "PASS" if sqlite_ok else "FAIL", "review": rv.verdict.value,
                    "layer2": layer2.value, "tests": tests_v.value, "rollback": "PASS"},
            review_note=rv.summary or rv.detail))
        store.record_attempt(target, source, "PENDING_APPROVAL", code)
        return Outcome.PENDING_APPROVAL

    def _clean_staging(self, store: Store) -> None:
        keep = set()
        pend = store.pending_candidate()
        if pend is not None:
            keep.add(str(Path(json.loads(pend["payload"])["candidate"]["repo"]).parent))
        root = self.cfg.work_dir / "staging"
        for d in root.iterdir() if root.is_dir() else []:
            if str(d) not in keep:
                shutil.rmtree(d, ignore_errors=True)

    # --- aplicacion aprobada ----------------------------------------------------------
    def apply(self, candidate_id: Optional[str] = None, trigger: str = "approval") -> tuple[Optional[int], Outcome]:
        return self._guarded("apply", trigger, lambda s, r: self._apply(s, r, candidate_id))

    def _apply(self, store: Store, rid: int, candidate_id: Optional[str]) -> Outcome:
        cfg = self.cfg
        self._to(store, rid, State.AUTHORIZATION_GATE)
        row = store.candidate(candidate_id) if candidate_id else store.pending_candidate()
        if row is None:
            store.check(rid, "approval", "candidate", Verdict.BLOCKED, detail="no hay candidato pendiente")
            return Outcome.BLOCKED
        payload = json.loads(row["payload"])
        code = row["id"]
        if row["status"] != "PENDING_APPROVAL":
            store.check(rid, "approval", "candidate", Verdict.BLOCKED, detail=f"el candidato {code} esta {row['status']}")
            return Outcome.BLOCKED
        if approval.candidate_code(payload) != code:
            store.check(rid, "approval", "candidate", Verdict.BLOCKED, detail="el payload guardado no corresponde a su codigo")
            return Outcome.BLOCKED
        ok, why, _ = approval.check(cfg.approvals_dir, payload, owner_uid=cfg.approvals_owner_uid,
                                    approval_ttl_s=cfg.policy.approval_ttl_s, now=self.clock())
        store.check(rid, "approval", "attestation", Verdict.PASS if ok else Verdict.BLOCKED, detail=why)
        if not ok:
            if "caducado" in why and "candidato" in why:
                store.set_candidate_status(code, "EXPIRED")
            notify.enqueue(store, rid, f"approval-denied:{code}:{rid}", "blocked",
                           f"HERMES — Aprobación no válida\n\nCandidato: {code}\nMotivo: {why}\n\nNo se ha tocado producción.")
            return Outcome.BLOCKED
        if not store.consume_approval(code, code, rid):
            store.check(rid, "approval", "single_use", Verdict.BLOCKED, detail="esta aprobacion ya se uso")
            return Outcome.BLOCKED
        store.check(rid, "approval", "single_use", Verdict.PASS, detail="aprobacion consumida (un solo uso)")
        store.merge_data(rid, candidate_id=code, payload=payload)

        def persist(state: str, data: dict) -> None:
            st = State(state)
            if State(store.run(rid)["state"]) != st:
                self._to(store, rid, st)
            store.merge_data(rid, **data)

        self._to(store, rid, State.CONTROLLED_DEPLOYMENT)
        res = self.ops.deploy(cfg, self.runner, payload, self.svc, persist=persist, sleep=self.sleep)
        store.merge_data(rid, deploy=res.to_dict())
        for s in res.steps:
            store.check(rid, "deploy", s.name, s.verdict, detail=s.detail)

        # Telegram: se prueba la ruta real ANTES del informe (el informe no puede certificarse a si mismo)
        tg_ok, tg_why = self.transport.send(f"HERMES — despliegue {code}: verificando ruta de Telegram")
        store.check(rid, "deploy", "telegram_route", Verdict.PASS if tg_ok else Verdict.FAIL, detail=tg_why)

        if not res.deployed:
            store.set_candidate_status(code, "BLOCKED")
            outcome = Outcome.BLOCKED
        elif res.rolled_back is None and res.post_ok:
            store.set_candidate_status(code, "DEPLOYED")
            outcome = Outcome.SUCCESS if tg_ok else Outcome.FAILED
        elif res.recovery_ok:
            store.set_candidate_status(code, "ROLLED_BACK")
            outcome = Outcome.ROLLED_BACK
        else:
            store.set_candidate_status(code, "ROLLED_BACK")
            outcome = Outcome.RECOVERY_FAILED
            notify.enqueue(store, rid, f"incident:{rid}", "incident", report.incident(
                phase=store.run(rid)["state"], cause="la actualizacion fallo y el rollback no dejo el servicio sano",
                actions=[f"{s.name}: {s.verdict.value} {s.detail[:120]}" for s in res.steps[-8:]],
                current="gateway NO verificado; acciones automaticas detenidas",
                evidence=f"run {rid}; hard rollback manual: hermes-updater rollback --run {rid} --hard"))
        self._to(store, rid, State.TELEGRAM_REPORT)
        after = versions.hermes_installed(cfg, self.runner)
        steps = {s.name: s.verdict.value for s in res.steps}
        errors = [f"{s.name}: {s.detail}" for s in res.steps if s.verdict not in (Verdict.PASS,)]
        checks = {
            "Backup": steps.get("preupdate_backup", "NOT_OBSERVED"),
            "Integración": steps.get("code", "NOT_OBSERVED"),
            "Tests": "PASS" if res.post_ok else ("FAIL" if res.post_ok is False else "NOT_OBSERVED"),
            "Gateway": steps.get("service_recovery", "NOT_OBSERVED"),
            "Telegram": "PASS" if tg_ok else "FAIL",
            "Persistencia": steps.get("post_persistence", "NOT_OBSERVED"),
            "Rollback disponible": "PASS" if res.rollback_point.get("venv_backup") else "NOT_OBSERVED",
        }
        state_label = {Outcome.SUCCESS: "SUCCESS", Outcome.ROLLED_BACK: "ROLLED_BACK (recuperado)",
                       Outcome.RECOVERY_FAILED: "RECOVERY_FAILED — INCIDENTE", Outcome.BLOCKED: "BLOCKED (sin cambios)",
                       Outcome.FAILED: "FAILED"}[outcome]
        notify.enqueue(store, rid, f"final:{rid}", "final", report.final(
            versions_before={"hermes": payload["source"]["version"]},
            versions_after={"hermes": payload["target"]["tag"] if outcome == Outcome.SUCCESS else
                            f"{payload['source']['version']} ({(after.get('commit') or '?')[:10]})"},
            checks=checks, aux={}, errors=errors, warnings=[], state=state_label))
        if outcome == Outcome.SUCCESS:
            self._to(store, rid, State.COMPLETE)
        return outcome

    # --- consulta -------------------------------------------------------------------
    def status(self) -> dict:
        ensure_dirs(self.cfg)
        store = Store(self.cfg.db_path, clock=self.clock)
        try:
            runs = [dict(r) for r in store.last_runs(10)]
            for r in runs:
                r["data"] = None
            pend = store.pending_candidate()
            return {"runs": runs, "pending_candidate": dict(pend) if pend else None,
                    "outbox_pending": len(store.unsent())}
        finally:
            store.close()


def _requery(cfg: Config, runner: Runner, product: str, **kw) -> Optional[str]:
    """Version instalada tras una actualizacion: solo --version, sin red."""
    binary = versions._which(product, kw.get("path_env"))
    if binary is None:
        return None
    r = runner.run([str(binary), "--version"], timeout=30, env={"NO_COLOR": "1", "CI": "1"})
    v = versions.semver(r.out) if r.ok else None
    return ".".join(map(str, v)) if v else None


def _upstream_base(cfg: Config, runner: Runner, source: str, target_commit: str) -> Optional[str]:
    """Prerrequisito del bundle de commits propios: la base upstream que ya esta
    en produccion (refs/remotes/origin/main local), si contiene al origen."""
    r = runner.run(["git", "-C", str(cfg.prod_repo), "merge-base", source, "refs/remotes/origin/main"], timeout=60)
    return (r.out.strip() or None) if r.ok else None


def _mem_available_mb() -> Optional[int]:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


def _maint_last(cfg: Config) -> dict:
    """Lectura de hermes-maint (no se cambia su semantica ni su estado)."""
    try:
        st = json.loads((cfg.hermes_home / "hermes-maint" / "state.json").read_text())
        last = (st.get("history") or [{}])[0]
        bf = next((t for t in last.get("tasks", []) if t.get("id") == "backup-freshness"), {})
        return {"outcome": last.get("outcome"), "detail": bf.get("detail", "")}
    except (OSError, ValueError, IndexError, AttributeError):
        return {"outcome": None}
