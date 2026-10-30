"""Stage 8.2 is disposable-only. Production indexing needs a future explicit change/review."""

import re

from sqlalchemy import text

PROTECTED_CLUSTERS = {"7696627159100764198", "7699165401457610790"}
DISPOSABLE_MARKER = "QUICKLIST_STAGE82_DISPOSABLE"


class IndexingTargetError(RuntimeError):
    pass


def verify_indexing_target(engine, expected_cluster):
    url = engine.url
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "127.0.0.1"
        or url.port != 55434
        or not re.fullmatch(r"stage82_[a-z0-9_]+_test", url.database or "")
        or url.query
        or not expected_cluster
        or expected_cluster in PROTECTED_CLUSTERS
    ):
        raise IndexingTargetError(
            "Indexing target is not an explicitly approved disposable endpoint"
        )
    with engine.connect() as connection, connection.begin():
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        identity = str(connection.scalar(text("SELECT system_identifier FROM pg_control_system()")))
        database = connection.scalar(text("SELECT current_database()"))
        marker = connection.scalar(
            text(
                "SELECT shobj_description(oid,'pg_database') FROM pg_database "
                "WHERE datname=current_database()"
            )
        )
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        vector = connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname='vector'")
        )
    if (
        identity != expected_cluster
        or identity in PROTECTED_CLUSTERS
        or database != url.database
        or marker != DISPOSABLE_MARKER
        or revision not in {"0007", "0008", "0009"}
        or vector != "0.8.7"
    ):
        raise IndexingTargetError(
            "Disposable cluster/database/marker/migration/vector verification failed"
        )
    return identity


def verify_worker_target(engine, settings):
    """Separate activation path; NEVER relax the destructive-test disposable guard."""
    if settings.app_environment != 'local' and settings.embedding_production_indexing_approved:
        url = engine.url
        if (url.host != 'postgres' or url.port != 5432 or url.database != 'quicklist'
                or url.query or url.drivername != 'postgresql+psycopg'
                or not settings.embedding_allowed_cluster_id
                or settings.embedding_allowed_cluster_id in PROTECTED_CLUSTERS
                or settings.embedding_model != 'text-embedding-3-large'
                or settings.embedding_dimensions != 1536):
            raise IndexingTargetError('Production endpoint/profile mismatch')
        with engine.connect() as connection, connection.begin():
            connection.exec_driver_sql('SET TRANSACTION READ ONLY')
            identity = str(connection.scalar(text('SELECT system_identifier FROM pg_control_system()')))
            marker = connection.scalar(text("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"))
            revision = connection.scalar(text('SELECT version_num FROM alembic_version'))
            vector = connection.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
        if (identity != settings.embedding_allowed_cluster_id or revision != '0009' or vector != '0.8.7'
                or marker != 'QUICKLIST_' + settings.app_environment.upper()):
            raise IndexingTargetError('Production cluster/marker/schema/vector mismatch')
        return identity
    if not settings.embedding_production_indexing_approved:
        return verify_indexing_target(engine, settings.embedding_allowed_cluster_id)
    url = engine.url
    cluster = "7699165401457610790"
    if (url.drivername != "postgresql+psycopg" or url.host not in {"127.0.0.1", "localhost"}
            or url.port != 5432 or url.database != "assistant" or url.query
            or settings.embedding_allowed_cluster_id != cluster
            or settings.embedding_model != "text-embedding-3-large"
            or settings.embedding_dimensions != 1536):
        raise IndexingTargetError("Approved worker target/profile does not match pinned primary")
    with engine.connect() as connection, connection.begin():
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        identity = str(connection.scalar(text("SELECT system_identifier FROM pg_control_system()")))
        database = connection.scalar(text("SELECT current_database()"))
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        vector = connection.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
    if (identity != cluster or database != "assistant" or revision not in {"0007", "0008", "0009"} or vector != "0.8.7"):
        raise IndexingTargetError("Primary identity/migration/vector verification failed")
    return identity
