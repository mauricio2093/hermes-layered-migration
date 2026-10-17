"""Estado persistente: ejecuciones, transiciones, comprobaciones, candidatos,
aprobaciones consumidas y la bandeja de salida de notificaciones.

Una sola base SQLite (WAL) fuera de ``$HERMES_HOME``. Todo lo que el informe
final afirma sale de aqui, no de variables en memoria: si el proceso muere a
mitad, la siguiente ejecucion sabe exactamente donde se quedo.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .status import DEPLOY_STATES, Outcome, State, Verdict

SCHEMA = 1

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                 -- daily | apply | manual | recover
    trigger TEXT NOT NULL,              -- timer | manual | approval | test
    state TEXT NOT NULL,
    outcome TEXT,                       -- NULL mientras esta abierta
    started_at REAL NOT NULL,
    finished_at REAL,
    heartbeat_at REAL NOT NULL,
    pid INTEGER NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS transitions (
    run_id INTEGER NOT NULL, at REAL NOT NULL, from_state TEXT, to_state TEXT NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS checks (
    run_id INTEGER NOT NULL, component TEXT NOT NULL, name TEXT NOT NULL,
    verdict TEXT NOT NULL, required INTEGER NOT NULL, detail TEXT, evidence TEXT, at REAL NOT NULL,
    PRIMARY KEY (run_id, component, name)
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,                -- sha256 corto del contenido que se aprueba
    run_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL,               -- PENDING_APPROVAL | DEPLOYED | ROLLED_BACK | EXPIRED | SUPERSEDED | BLOCKED
    payload TEXT NOT NULL               -- JSON canonico (lo que firma la aprobacion)
);
CREATE TABLE IF NOT EXISTS attempts (    -- antibucle: que objetivo se intento y como acabo
    target TEXT NOT NULL, source TEXT NOT NULL, at REAL NOT NULL, outcome TEXT NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS consumed_approvals (
    code TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, run_id INTEGER NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER, dedup_key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
    created_at REAL NOT NULL, sent_at REAL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
"""


class Busy(RuntimeError):
    """Otra ejecucion tiene el candado."""


@contextmanager
def exclusive(lock_path: Path) -> Iterator[int]:
    """flock no bloqueante. El kernel lo suelta si el proceso muere: un candado
    huerfano no puede dejar el sistema parado."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy(str(lock_path)) from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield fd
    finally:
        os.close(fd)


class Store:
    def __init__(self, path: Path, *, clock=time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.clock = clock
        self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(DDL)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
        os.chmod(path, 0o600)

    def close(self) -> None:
        self.db.close()

    # --- ejecuciones -----------------------------------------------------
    def open_run(self, kind: str, trigger: str) -> int:
        now = self.clock()
        cur = self.db.execute(
            "INSERT INTO runs (kind, trigger, state, started_at, heartbeat_at, pid) VALUES (?,?,?,?,?,?)",
            (kind, trigger, State.SCHEDULED.value, now, now, os.getpid()))
        rid = cur.lastrowid
        self.db.execute("INSERT INTO transitions VALUES (?,?,?,?,?)",
                        (rid, now, None, State.SCHEDULED.value, f"{kind}/{trigger}"))
        return rid

    def transition(self, run_id: int, to: State, note: str = "") -> None:
        now = self.clock()
        row = self.db.execute("SELECT state, outcome FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        if row["outcome"] is not None:
            raise RuntimeError(f"la ejecucion {run_id} ya esta cerrada ({row['outcome']})")
        self.db.execute("UPDATE runs SET state=?, heartbeat_at=? WHERE id=?", (to.value, now, run_id))
        self.db.execute("INSERT INTO transitions VALUES (?,?,?,?,?)",
                        (run_id, now, row["state"], to.value, note[:500]))

    def heartbeat(self, run_id: int) -> None:
        self.db.execute("UPDATE runs SET heartbeat_at=? WHERE id=?", (self.clock(), run_id))

    def close_run(self, run_id: int, outcome: Outcome) -> None:
        """Cierra una vez. Cerrar como verde exige que no haya comprobaciones
        obligatorias en otro estado que PASS: un fallo nunca queda como exito."""
        if outcome in (Outcome.SUCCESS, Outcome.NO_UPDATES):
            bad = self.required_not_pass(run_id)
            if bad:
                raise RuntimeError(
                    f"no se puede cerrar como {outcome.value}: obligatorias sin PASS: "
                    + ", ".join(f"{c}/{n}={v}" for c, n, v in bad))
        cur = self.db.execute("UPDATE runs SET outcome=?, finished_at=? WHERE id=? AND outcome IS NULL",
                              (outcome.value, self.clock(), run_id))
        if cur.rowcount != 1:
            raise RuntimeError(f"la ejecucion {run_id} ya estaba cerrada")

    def run(self, run_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def data(self, run_id: int) -> dict:
        return json.loads(self.run(run_id)["data"])

    def merge_data(self, run_id: int, **kv: Any) -> dict:
        d = self.data(run_id)
        d.update(kv)
        self.db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(d, default=str, sort_keys=True), run_id))
        return d

    def open_runs(self) -> list:
        return self.db.execute("SELECT * FROM runs WHERE outcome IS NULL ORDER BY id").fetchall()

    def last_runs(self, n: int = 10) -> list:
        return self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    def transitions(self, run_id: int) -> list:
        return self.db.execute("SELECT * FROM transitions WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()

    # --- comprobaciones ----------------------------------------------------
    def check(self, run_id: int, component: str, name: str, verdict: Verdict, *,
              required: bool = True, detail: str = "", evidence: Any = None) -> Verdict:
        verdict = Verdict(verdict)
        self.db.execute(
            "INSERT OR REPLACE INTO checks VALUES (?,?,?,?,?,?,?,?)",
            (run_id, component, name, verdict.value, int(required), detail[:2000],
             json.dumps(evidence, default=str)[:20000] if evidence is not None else None, self.clock()))
        return verdict

    def checks(self, run_id: int) -> list:
        return self.db.execute("SELECT * FROM checks WHERE run_id=? ORDER BY at, component, name",
                               (run_id,)).fetchall()

    def required_not_pass(self, run_id: int) -> list:
        return [(r["component"], r["name"], r["verdict"]) for r in self.db.execute(
            "SELECT * FROM checks WHERE run_id=? AND required=1 AND verdict<>'PASS'", (run_id,))]

    # --- candidatos y antibucle ---------------------------------------------
    def save_candidate(self, cid: str, run_id: int, payload: dict, ttl_s: float) -> None:
        now = self.clock()
        # un candidato nuevo deja obsoletos a los pendientes anteriores
        self.db.execute("UPDATE candidates SET status='SUPERSEDED' WHERE status='PENDING_APPROVAL' AND id<>?", (cid,))
        self.db.execute("INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?,?)",
                        (cid, run_id, now, now + ttl_s, "PENDING_APPROVAL",
                         json.dumps(payload, sort_keys=True, separators=(",", ":"))))

    def candidate(self, cid: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()

    def pending_candidate(self) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM candidates WHERE status='PENDING_APPROVAL' ORDER BY created_at DESC LIMIT 1").fetchone()

    def set_candidate_status(self, cid: str, status: str) -> None:
        self.db.execute("UPDATE candidates SET status=? WHERE id=?", (status, cid))

    def record_attempt(self, target: str, source: str, outcome: str, reason: str = "") -> None:
        self.db.execute("INSERT INTO attempts VALUES (?,?,?,?,?)", (target, source, self.clock(), outcome, reason[:500]))

    def last_attempt(self, target: str, source: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM attempts WHERE target=? AND source=? ORDER BY at DESC LIMIT 1",
                               (target, source)).fetchone()

    def consume_approval(self, code: str, candidate_id: str, run_id: int) -> bool:
        """True si es la primera vez. Un codigo de aprobacion se usa UNA vez."""
        try:
            self.db.execute("INSERT INTO consumed_approvals VALUES (?,?,?,?)",
                            (code, candidate_id, run_id, self.clock()))
            return True
        except sqlite3.IntegrityError:
            return False

    # --- bandeja de salida ---------------------------------------------------
    def enqueue(self, run_id: Optional[int], dedup_key: str, kind: str, body: str) -> bool:
        try:
            self.db.execute("INSERT INTO outbox (run_id, dedup_key, kind, body, created_at) VALUES (?,?,?,?,?)",
                            (run_id, dedup_key, kind, body, self.clock()))
            return True
        except sqlite3.IntegrityError:
            return False      # ya encolado: no se duplica

    def unsent(self, max_attempts: int = 50) -> list:
        return self.db.execute("SELECT * FROM outbox WHERE sent_at IS NULL AND attempts < ? ORDER BY id",
                               (max_attempts,)).fetchall()

    def mark_sent(self, oid: int) -> None:
        self.db.execute("UPDATE outbox SET sent_at=?, attempts=attempts+1, last_error=NULL WHERE id=?", (self.clock(), oid))

    def mark_failed(self, oid: int, error: str) -> None:
        self.db.execute("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?", (error[:500], oid))


def interrupted(store: Store) -> list:
    """Ejecuciones abiertas cuyo proceso ya no existe. Solo se llama con el
    candado tomado, asi que ninguna de ellas puede seguir viva legitimamente."""
    out = []
    for r in store.open_runs():
        if r["pid"] == os.getpid():
            continue
        out.append(r)
    return out


def needs_reconcile(row) -> bool:
    return State(row["state"]) in DEPLOY_STATES
