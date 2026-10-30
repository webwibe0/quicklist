"""Atomic, idempotent import. Run with the backend virtual environment."""
import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.ownership import bind_owner  # noqa: E402
from app.models.accounts import User  # noqa: E402
from app.services.dataset import DEFAULT_CSV, import_dataset, infer_type, read_rows  # noqa: E402, F401


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--owner-id", required=True, type=UUID)
    args = parser.parse_args()
    engine = create_engine(get_settings().database_url)
    try:
        with Session(engine) as session, session.begin():
            if session.get(User, args.owner_id) is None:
                raise ValueError("Import owner does not exist")
            bind_owner(session, args.owner_id)
            report = import_dataset(session, args.path)
        print(json.dumps(report, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
