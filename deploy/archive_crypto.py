"""Bounded first-release backup encryption; keys never enter argv, images or logs."""
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

LIMIT = 64 * 1024 * 1024


def transform(mode, source, destination, key_path):
    source, destination = Path(source), Path(destination)
    if mode not in {'encrypt', 'decrypt'} or source.stat().st_size > LIMIT:
        raise ValueError('Invalid operation or archive too large; reviewed streaming backup required')
    cipher = Fernet(Path(key_path).read_bytes().strip())
    # ponytail: bounded whole-file encryption for the current ~3MB database;
    # migrate to a reviewed streaming backup tool before archives reach 64MiB.
    value = cipher.encrypt(source.read_bytes()) if mode == 'encrypt' else cipher.decrypt(source.read_bytes())
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(value)


if __name__ == '__main__':
    transform(*sys.argv[1:])
    print('Backup cryptographic operation complete; no keys or contents displayed')
