"""hermes-updater — CLI.

  hermes-updater run [--trigger manual|timer] [--no-aux] [--no-prepare] [--notify file|none|hermes-send]
  hermes-updater versions [--json]
  hermes-updater backup [--verify DIR]
  hermes-updater apply [--candidate ID]          (exige aprobacion atestiguada por TOTP)
  hermes-updater rollback --run N [--hard]       (manual; --hard restaura las bases)
  hermes-updater status | flush | checks --run N

Codigos de salida (como hermes-maint: un hallazgo nunca se pinta de verde):
  0 SUCCESS / NO_UPDATES     3 otra ejecucion tiene el candado
  4 PENDING_APPROVAL         6 BLOCKED      7 FAILED
  8 ROLLED_BACK              9 RECOVERY_FAILED (incidente)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from . import backup, config, deploy, notify, verify, versions
from .machine import Orchestrator
from .proc import Runner
from .status import Outcome
from .store import Store

EXIT = {Outcome.SUCCESS: 0, Outcome.NO_UPDATES: 0, Outcome.PENDING_APPROVAL: 4, Outcome.BLOCKED: 6,
        Outcome.FAILED: 7, Outcome.INTERRUPTED: 7, Outcome.ROLLED_BACK: 8, Outcome.RECOVERY_FAILED: 9}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hermes-updater")
    ap.add_argument("--config", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--trigger", default="manual", choices=["manual", "timer"])
    r.add_argument("--no-aux", action="store_true")
    r.add_argument("--no-prepare", action="store_true")
    r.add_argument("--notify", choices=["hermes-send", "file", "none"])
    v = sub.add_parser("versions")
    v.add_argument("--json", action="store_true")
    b = sub.add_parser("backup")
    b.add_argument("--verify", type=Path)
    a = sub.add_parser("apply")
    a.add_argument("--candidate")
    rb = sub.add_parser("rollback")
    rb.add_argument("--run", type=int, required=True)
    rb.add_argument("--hard", action="store_true")
    sub.add_parser("status")
    sub.add_parser("flush")
    c = sub.add_parser("checks")
    c.add_argument("--run", type=int, required=True)
    args = ap.parse_args(argv)

    cfg = config.load(args.config)
    if getattr(args, "notify", None):
        cfg = replace(cfg, policy=replace(cfg.policy, notify_transport=args.notify))
    runner = Runner()

    if args.cmd == "run":
        orch = Orchestrator(cfg, runner=runner, enable_aux=not args.no_aux, enable_prepare=not args.no_prepare)
        rid, out = orch.daily(trigger=args.trigger)
        if rid is None:
            print("otra ejecucion tiene el candado")
            return 3
        print(f"run {rid}: {out.value}")
        _print_checks(cfg, rid)
        return EXIT[out]
    if args.cmd == "apply":
        rid, out = Orchestrator(cfg, runner=runner).apply(args.candidate)
        if rid is None:
            return 3
        print(f"run {rid}: {out.value}")
        _print_checks(cfg, rid)
        return EXIT[out]
    if args.cmd == "versions":
        res = versions.discover(cfg, runner)
        if args.json:
            print(json.dumps({k: v.to_dict() for k, v in res.items()}, indent=2, default=str))
        else:
            for k, pv in res.items():
                print(f"{k:9} {pv.extra.get('display') or pv.installed or '?':32} -> {pv.available or '?':10} "
                      f"{pv.status_label:24} {pv.verdict.value:17} {pv.method or ''}")
                if pv.detail:
                    print(f"          {pv.detail}")
        return 0
    if args.cmd == "backup":
        config.ensure_dirs(cfg)
        if args.verify:
            rep = verify.verify_backup(args.verify, hermes_home=cfg.hermes_home, work_dir=cfg.work_dir,
                                       prod_repo=cfg.prod_repo, gateway_unit=cfg.gateway_unit)
            print(json.dumps(rep.to_dict(), indent=2, default=str))
            return 0 if rep.verdict.value == "PASS" else 7
        res = backup.run_backup_a(cfg, runner)
        print(json.dumps({k: res.to_dict()[k] for k in ("verdict", "backup_dir", "backup_id", "detail", "evidence_path")}, indent=2))
        return 0 if res.verdict.value == "PASS" else 7
    if args.cmd == "status":
        print(json.dumps(Orchestrator(cfg, runner=runner).status(), indent=2, default=str))
        return 0
    if args.cmd == "flush":
        config.ensure_dirs(cfg)
        st = Store(cfg.db_path)
        try:
            print(json.dumps(notify.flush(st, notify.transport_for(cfg, runner), retries=cfg.policy.notify_retries)))
        finally:
            st.close()
        return 0
    if args.cmd == "checks":
        _print_checks(cfg, args.run)
        return 0
    if args.cmd == "rollback":
        st = Store(cfg.db_path)
        try:
            data = st.data(args.run)
        finally:
            st.close()
        rp = data.get("rollback_point")
        if not rp:
            print("esa ejecucion no tiene punto de rollback")
            return 6
        svc = deploy.SystemdUser(runner, cfg.gateway_unit)
        if args.hard:
            restored = deploy.hard_restore_databases(cfg, svc, rp)
            print("bases restauradas desde el snapshot pre-despliegue:", ", ".join(restored))
        res = deploy.DeployResult(rollback_point=rp)
        ok = deploy.rollback(cfg, runner, svc, rp, res)
        healthy, why = deploy.wait_healthy(svc, timeout_s=cfg.policy.service_ready_timeout_s,
                                           stable_s=cfg.policy.service_stable_s)
        for s in res.steps:
            print(f"{s.verdict.value:8} {s.name:20} {s.detail}")
        print(f"servicio: {why}")
        return 0 if ok and healthy else 9
    return 2


def _print_checks(cfg, rid: int) -> None:
    st = Store(cfg.db_path)
    try:
        for c in st.checks(rid):
            req = "" if c["required"] else " (opcional)"
            print(f"  {c['verdict']:17} {c['component']}/{c['name']}{req}: {(c['detail'] or '')[:220]}")
        print("  estados: " + " -> ".join(t["to_state"] for t in st.transitions(rid)))
    finally:
        st.close()


if __name__ == "__main__":
    sys.exit(main())
