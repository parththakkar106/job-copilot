#!/usr/bin/env python3
"""Set the web console's password. Also logs out every open session.

    .venv\\Scripts\\python.exe scripts\\set_console_password.py

The password is typed at a hidden prompt, so it never lands in shell history. Only its
scrypt hash is stored, in the gitignored bridge/.console_auth.json.
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web.app import auth  # noqa: E402


def main() -> int:
    password = getpass.getpass(f"New console password ({auth.MIN_PASSWORD}+ characters): ")
    if password != getpass.getpass("Type it again: "):
        print("The two entries differ. Nothing changed.", file=sys.stderr)
        return 1
    try:
        auth.write_password(password)
    except ValueError as e:
        print(f"{e}. Nothing changed.", file=sys.stderr)
        return 1
    print(f"Password set. Hash stored in {auth.AUTH_PATH}. Restart with: python start.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
