"""Read downloaded Web OAuth JSON locally. No secrets in CLI arguments or output."""

import argparse
import base64
import csv
import io
import os
import re
import subprocess
import tempfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
REDIRECT = "http://127.0.0.1:8000/api/v1/calendar/google/callback"


def private_file(path):
    if os.name != "nt":
        path.chmod(0o600)
        return
    import ctypes
    from ctypes import wintypes

    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True
    )
    sid = next(csv.reader(io.StringIO(result.stdout)))[1]
    if not re.fullmatch(r"S-1-[0-9-]+", sid):
        raise ValueError("Local identity not verified")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = api.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    convert.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    if not convert(f"D:P(A;;FA;;;SY)(A;;FA;;;{sid})", 1, ctypes.byref(descriptor), None):
        raise OSError("Cannot prepare private configuration permissions")
    try:
        setter = api.SetFileSecurityW
        setter.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        setter.restype = wintypes.BOOL
        if not setter(str(path), 0x80000004, descriptor):
            raise OSError("Cannot protect private configuration")
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(descriptor)


def configure(client_file, env_file, *, write=False):
    import json

    web = json.loads(Path(client_file).read_text(encoding="utf-8"))["web"]
    client_id, secret = web["client_id"], web["client_secret"]
    if REDIRECT not in web.get("redirect_uris", []) or not client_id or not secret:
        raise ValueError("Web OAuth client and exact redirect URI required")
    previous = Path(env_file).read_text(encoding="utf-8") if Path(env_file).exists() else ""
    values = dotenv_values(stream=io.StringIO(previous))
    key = values.get("GOOGLE_TOKEN_KEY") or base64.urlsafe_b64encode(os.urandom(32)).decode()
    if len(base64.urlsafe_b64decode(key)) != 32:
        raise ValueError("Existing Google token key is invalid; never replace it silently")
    updates = {
        "GOOGLE_CALENDAR_ENABLED": "true",
        "GOOGLE_CLIENT_ID": client_id,
        "GOOGLE_CLIENT_SECRET": secret,
        "GOOGLE_TOKEN_KEY": key,
        "GOOGLE_REDIRECT_URI": REDIRECT,
    }
    for value in updates.values():
        if any(c in value for c in "\r\n'"):
            raise ValueError("Unexpected multiline credential")
    lines = previous.splitlines(keepends=True)
    kept = [line for line in lines if not any(line.startswith(name + "=") for name in updates)]
    content = (
        "".join(kept).rstrip("\r\n")
        + "\n"
        + "".join(f"{name}='{value}'\n" for name, value in updates.items())
    )
    after = dotenv_values(stream=io.StringIO(content))
    if {k: v for k, v in values.items() if k not in updates} != {
        k: v for k, v in after.items() if k not in updates
    }:
        raise ValueError("Unrelated configuration would change")
    if not write:
        return False
    env_file = Path(env_file).resolve()
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=env_file.parent, prefix=".google-config-", delete=False
    ) as handle:
        temporary = Path(handle.name)
        private_file(temporary)
        handle.write(content)
    try:
        temporary.replace(env_file)
    finally:
        temporary.unlink(missing_ok=True)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-file", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--write", action="store_true", help="Explicitly write only Google configuration"
    )
    args = parser.parse_args()
    try:
        changed = configure(args.client_file, args.env_file, write=args.write)
        print(
            "Google configuration saved privately; unrelated settings preserved."
            if changed
            else "OAuth file verified. No configuration written; use --write after deployment approval."
        )
    except Exception as error:
        print("Google configuration not changed:", type(error).__name__)
        raise SystemExit(2) from None
