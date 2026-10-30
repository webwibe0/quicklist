"""One lifecycle-bound worker per database; existing leases/cache handle restart recovery."""

import logging
from threading import Event, Thread

from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError

from app.ai.embeddings import EmbeddingError, OpenAIEmbeddingProvider
from app.core.spend_budget import BudgetBlocked, SpendBudget
from app.services.indexing import IndexingWorker
from app.services.indexing_target import verify_worker_target

logger = logging.getLogger(__name__)


class AutomaticIndexing:
    def __init__(self, engine, settings):
        self.engine, self.settings = engine, settings
        self.stop_event = Event()
        self.thread = None
        self.status = "disabled"

    def start(self):
        if not self.settings.embedding_worker_enabled:
            return
        verify_worker_target(self.engine, self.settings)
        if (not self.settings.embedding_real_api_enabled
                or (self.settings.ai_usage_mode == 'capped' and self.settings.embedding_budget_usd <= 0)):
            self.status = "budget_blocked"
            return
        self.thread = Thread(target=self.run, name="quicklist-indexing", daemon=True)
        self.thread.start()

    def run(self):
        provider = None
        try:
            budget = SpendBudget(
                self.settings.embedding_budget_path,
                limit_nanos=round(self.settings.embedding_budget_usd * 1_000_000_000),
                max_calls=self.settings.embedding_budget_max_calls,
                min_interval=self.settings.embedding_min_call_interval_seconds,
                enforce_limits=self.settings.ai_usage_mode == 'capped',
            )
            # Session advisory lock survives commits; AUTOCOMMIT avoids a long transaction.
            with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
                if not lock.scalar(
                    text("SELECT pg_try_advisory_lock(hashtext('quicklist-auto-indexing'))")
                ):
                    self.status = "another_worker"
                    return
                try:
                    provider = OpenAIEmbeddingProvider(
                        self.settings, allow_paid_api=True, budget=budget
                    )
                    worker = IndexingWorker(self.engine, provider, self.settings)
                    self.status = "running"
                    while not self.stop_event.is_set():
                        try:
                            stats = worker.run_once(stop=self.stop_event, changes_only=True)
                        except (OperationalError, InterfaceError):
                            # Claims are durable and their leases are recovered after reconnect.
                            self.status = "database_unavailable"
                            logger.warning("Automatic indexing deferred: database unavailable")
                            if self.stop_event.wait(self.settings.embedding_poll_seconds):
                                break
                            continue
                        if stats.get("budget_blocked"):
                            self.status = "budget_blocked"
                        else:
                            self.status = "running"
                        if self.stop_event.wait(self.settings.embedding_poll_seconds):
                            break
                finally:
                    lock.execute(
                        text("SELECT pg_advisory_unlock(hashtext('quicklist-auto-indexing'))")
                    )
        except BudgetBlocked:
            self.status = "budget_blocked"
            logger.warning("Automatic indexing stopped: durable budget unavailable")
        except (SQLAlchemyError, ValueError, EmbeddingError) as error:
            self.status = "error"
            logger.error(
                "Automatic indexing stopped: %s; pending database work retained",
                type(error).__name__,
            )
        finally:
            if provider:
                provider.close()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(4 * self.settings.embedding_timeout_seconds + 5)
        if not self.thread or not self.thread.is_alive():
            self.status = "stopped"
