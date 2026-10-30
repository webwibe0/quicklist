"""Durable pre-send reservations, in USD nanounits. No prompts, keys or DSNs stored."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class BudgetBlocked(RuntimeError):
    pass


class SpendBudget:
    def __init__(self, path, *, limit_nanos, max_calls, min_interval=0, enforce_limits=True):
        if (type(limit_nanos) is not int or limit_nanos < 0
                or type(max_calls) is not int or max_calls < 0 or min_interval < 0):
            raise BudgetBlocked("INVALID_LIMIT")
        self.path = Path(path).resolve()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise BudgetBlocked("BUDGET_STORAGE_UNAVAILABLE") from None
        self.limit, self.max_calls, self.interval = limit_nanos, max_calls, min_interval
        if type(enforce_limits) is not bool:
            raise BudgetBlocked("INVALID_LIMIT")
        self.enforce_limits = enforce_limits
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,reserved INTEGER NOT NULL,actual INTEGER,input_tokens INTEGER,output_tokens INTEGER,state TEXT NOT NULL,created REAL NOT NULL)"
            )
            binding = json.dumps([limit_nanos, max_calls, min_interval])
            row = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
            if row and row[0] != binding:
                raise BudgetBlocked("BUDGET_BINDING_CHANGED")
            db.execute("INSERT OR IGNORE INTO meta VALUES('binding',?)", (binding,))

    @contextmanager
    def transaction(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error:
            if db:
                db.rollback()
            raise BudgetBlocked("BUDGET_STORAGE_UNAVAILABLE") from None
        except BaseException:
            if db:
                db.rollback()
            raise
        finally:
            if db:
                db.close()

    def reserve(self, kind, nanos):
        if (
            type(nanos) is not int
            or nanos < 1
            or kind not in {"embedding", "nlu", "stt", "tts", "worker_allowance"}
        ):
            raise BudgetBlocked("INVALID_RESERVATION")
        with self.transaction() as db:
            used, count, last = db.execute(
                "SELECT coalesce(sum(coalesce(actual,reserved)),0),count(*),coalesce(max(created),0) FROM calls"
            ).fetchone()
            frozen = db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone()
            if frozen and (self.enforce_limits or frozen[0] != "usage exceeded reservation"):
                raise BudgetBlocked("BUDGET_FROZEN")
            if self.enforce_limits and (used + nanos > self.limit or (self.max_calls and count >= self.max_calls)):
                raise BudgetBlocked("BUDGET_EXHAUSTED")
            if time.time() - last < self.interval:
                raise BudgetBlocked("BUDGET_RATE_LIMIT")
            return db.execute(
                "INSERT INTO calls(kind,reserved,state,created) VALUES(?,?,?,?)",
                (kind, nanos, "unknown", time.time()),
            ).lastrowid

    def finish(self, ticket, actual_nanos, *, input_tokens=0, output_tokens=0):
        if type(actual_nanos) is not int or actual_nanos < 0:
            raise BudgetBlocked("INVALID_USAGE")
        exceeded = False
        with self.transaction() as db:
            row = db.execute("SELECT reserved,state FROM calls WHERE id=?", (ticket,)).fetchone()
            if row is None or row[1] != "unknown":
                raise BudgetBlocked("INVALID_TICKET")
            exceeded = actual_nanos > row[0]
            db.execute(
                "UPDATE calls SET actual=?,input_tokens=?,output_tokens=?,state=? WHERE id=?",
                (
                    actual_nanos,
                    input_tokens,
                    output_tokens,
                    "overrun" if exceeded else "complete",
                    ticket,
                ),
            )
            if exceeded and self.enforce_limits:
                db.execute(
                    "INSERT OR REPLACE INTO meta VALUES('frozen','usage exceeded reservation')"
                )
        if exceeded and self.enforce_limits:
            raise BudgetBlocked("USAGE_EXCEEDED_RESERVATION")

    def summary(self):
        with self.transaction() as db:
            used, count, unknown = db.execute(
                "SELECT coalesce(sum(coalesce(actual,reserved)),0),count(*),coalesce(sum(state='unknown'),0) FROM calls"
            ).fetchone()
            return {
                "limit_nanos": self.limit if self.enforce_limits else None,
                "committed_nanos": used,
                "calls": count,
                "unknown": unknown,
                "frozen": bool(db.execute("SELECT 1 FROM meta WHERE key='frozen'").fetchone()),
            }
