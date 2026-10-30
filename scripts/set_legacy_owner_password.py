"""Local terminal only. Run AFTER explicit data-migration approval, never with password args."""
import argparse
import getpass
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from sqlalchemy import delete, select, text  # noqa: E402

from app.core.passwords import hash_password  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.accounts import AuthSession, User  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--email', required=True)
    parser.add_argument('--expected-user-id', type=UUID, required=True)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise ValueError('Use a local interactive terminal; password stdin/files/arguments are not accepted')
    with SessionLocal() as session:
        if session.scalar(text('SELECT version_num FROM alembic_version')) != '0007':
            raise ValueError('Account migration 0007 must already be approved and applied')
        user = session.scalar(select(User).where(User.email == args.email.strip().casefold()))
        if user is None or user.id != args.expected_user_id or not user.is_legacy_owner:
            raise ValueError('Explicit original owner identity mismatch')
    password = getpass.getpass('Пароль первоначального владельца (не отображается): ')
    confirmation = getpass.getpass('Повторите пароль: ')
    if password != confirmation:
        raise ValueError('Passwords differ')
    encoded = hash_password(password)
    with SessionLocal.begin() as session:
        user = session.scalar(select(User).where(User.id == args.expected_user_id,
            User.email == args.email.strip().casefold(), User.is_legacy_owner.is_(True)).with_for_update())
        if user is None:
            raise ValueError('Original owner identity changed')
        user.password_hash = encoded
        session.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    print('Пароль установлен; прежние сессии владельца отозваны.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Операция не выполнена. Проверьте миграцию, владельца и пароль локально; секреты не выводятся.')
        raise SystemExit(2) from None
