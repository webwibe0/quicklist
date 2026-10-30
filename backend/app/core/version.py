"""Non-secret fingerprint of Python source; never reads configuration or .env."""
import hashlib
from pathlib import Path


def source_version():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


LOADED_CODE_VERSION = source_version()

if __name__ == "__main__":
    print(source_version())
