#!/usr/bin/env python3
"""Store an Instagram authenticator-app seed without attempting a login.

Useful for an existing installation that originally used SMS or no 2FA. The
seed is read from the terminal without echo and stored only in macOS Keychain.
"""
from __future__ import annotations

import getpass

import keyring
import pyotp

from auth import KEYRING_SERVICE


def main() -> int:
    username = keyring.get_password(KEYRING_SERVICE, "username")
    if not username:
        print("No Instagram username in Keychain. Run setup_auth.py first.")
        return 1

    seed = getpass.getpass("TOTP secret seed (hidden): ").replace(" ", "").upper()
    if len(seed) < 16 or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for char in seed):
        print("That does not look like a Base32 TOTP seed. Nothing was stored.")
        return 1

    try:
        code = pyotp.TOTP(seed).now()
    except Exception:
        print("The TOTP seed could not be read. Nothing was stored.")
        return 1

    keyring.set_password(KEYRING_SERVICE, f"totp:{username}", seed)
    print(f"TOTP seed stored in macOS Keychain. Current code generated: {code}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
