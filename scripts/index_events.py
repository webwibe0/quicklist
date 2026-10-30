"""Explicit bounded preview/worker; primary requires separate data/API approval. Never imported by FastAPI startup."""

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from threading import Event
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pydantic import ValidationError  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.ai.embeddings import (  # noqa: E402
    EmbeddingError,
    EmbeddingProfile,
    FakeEmbeddingProvider,
    OpenAIEmbeddingProvider,
)
from app.core.config import Settings, get_settings  # noqa: E402
from app.models.entities import Event as StoredEvent  # noqa: E402
from app.services.indexing import IndexingWorker  # noqa: E402
from app.services.indexing_target import IndexingTargetError, verify_worker_target  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url", default=os.environ.get("EMBEDDING_DATABASE_URL")
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--ack-disposable", action="store_true")
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--ack-primary-data", action="store_true")
    parser.add_argument(
        "--allow-paid-api",
        action="store_true",
        help="Requires separate approval, enabled real API and primary data acknowledgement",
    )
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--max-documents", type=int)
    parser.add_argument("--max-run-bytes", type=int)
    parser.add_argument("--after", type=UUID)
    parser.add_argument("--cycles", type=int, default=1)
    args = parser.parse_args()
    if not args.database_url:
        raise IndexingTargetError(
            "An explicit EMBEDDING_DATABASE_URL/--database-url is required"
        )
    settings = get_settings()
    overrides = {
        field: value
        for field, value in {
            "embedding_batch_size": args.batch_size,
            "embedding_max_documents": args.max_documents,
            "embedding_max_run_bytes": args.max_run_bytes,
        }.items()
        if value is not None
    }
    settings = Settings.model_validate(settings.model_dump() | overrides)
    if not 1 <= args.cycles <= 1000:
        raise ValueError("Invalid cycle limit")
    primary = settings.embedding_production_indexing_approved
    if args.execute:
        if not settings.embedding_worker_enabled:
            raise IndexingTargetError("Worker disabled")
        if primary:
            if (not args.ack_primary_data or not args.allow_paid_api or args.fake
                    or not settings.embedding_real_api_enabled):
                raise IndexingTargetError("Primary execution requires explicit data and paid API approval")
        elif not args.ack_disposable or not args.fake or args.allow_paid_api:
            raise IndexingTargetError("Disposable execution requires fake provider and acknowledgement")
    engine = create_engine(
        args.database_url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 3,
            "application_name": "quicklist-index-worker",
        },
    )
    try:
        identity = verify_worker_target(engine, settings)
        if not args.execute:
            with engine.connect() as connection, connection.begin():
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                count = connection.scalar(select(func.count()).select_from(StoredEvent))
            print(
                json.dumps(
                    {
                        "mode": "preview",
                        "mutations": 0,
                        "cluster": identity,
                        "events": count,
                        "worker_enabled": settings.embedding_worker_enabled,
                        "real_api_enabled": settings.embedding_real_api_enabled,
                        "max_documents": settings.embedding_max_documents,
                        "max_input_bytes": settings.embedding_max_input_bytes,
                        "max_run_bytes": settings.embedding_max_run_bytes,
                        "cycles": args.cycles,
                        "total_input_limit": settings.embedding_max_documents
                        * args.cycles,
                        "total_byte_limit": settings.embedding_max_run_bytes
                        * args.cycles,
                        "token_estimates_are_approximate": True,
                    }
                )
            )
            return 0
        stop = Event()

        def request_stop(signum, frame):
            stop.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        profile = EmbeddingProfile(
            settings.embedding_model, settings.embedding_dimensions
        )
        provider = (OpenAIEmbeddingProvider(settings, profile, allow_paid_api=True) if primary
                    else FakeEmbeddingProvider(profile=profile))
        worker = IndexingWorker(engine, provider, settings)
        if args.cycles == 1:
            print(
                json.dumps(
                    dict(mode="primary" if primary else "fake", **worker.run_once(after=args.after, stop=stop))
                )
            )
        else:
            for result in worker.run_periodic(stop, cycles=args.cycles):
                print(json.dumps(dict(mode="primary" if primary else "fake", **result)), flush=True)
        return 0
    finally:
        if "provider" in locals() and isinstance(provider, OpenAIEmbeddingProvider):
            provider.close()
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        IndexingTargetError,
        EmbeddingError,
        ValidationError,
        SQLAlchemyError,
        ValueError,
    ) as error:
        # Exception bodies can include credentials or private SQL values. Fixed codes only.
        code = error.code if isinstance(error, EmbeddingError) else type(error).__name__
        print(json.dumps({"status": "blocked", "code": code}))
        raise SystemExit(2) from None
