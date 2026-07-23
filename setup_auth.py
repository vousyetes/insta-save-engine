"""
setup_auth.py — ONE-TIME interactive setup for the Instagram login.

Run it once, in a terminal:

    cd ~/insta-save-engine
    .venv/bin/python setup_auth.py

It asks for your Instagram username/password (password is hidden), handles 2FA,
stores the credentials in the macOS Keychain (never in a file), establishes the
session, and saves it to ig_session.json. After this, sync.py logs in and
re-logs in by itself — you never have to touch a cookie again.

The password is typed straight into the Keychain; it is never printed, logged,
or written to disk in plain text.
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

import keyring
from instagrapi import Client
from instagrapi.exceptions import TwoFactorRequired

from auth import KEYRING_SERVICE, SESSION_FILE, EXPECTED_USER_ID


def main() -> int:
    print("── Insta Save Engine — Instagram auth setup ────────────────────────")
    if EXPECTED_USER_ID:
        print(f"Expected account user_id: {EXPECTED_USER_ID}")
    print()

    username = input("Instagram username: ").strip()
    if not username:
        print("Aborted: empty username.")
        return 1
    password = getpass.getpass("Instagram password (hidden): ")
    if not password:
        print("Aborted: empty password.")
        return 1
    print(
        "\nIf the account uses an authenticator app (TOTP), paste its secret SEED\n"
        "here so re-logins stay fully automatic. Leave empty for SMS/none."
    )
    totp_seed = getpass.getpass("TOTP secret seed (optional, hidden): ").strip()

    cl = Client()
    cl.delay_range = [1, 3]

    try:
        code = cl.totp_generate_code(totp_seed) if totp_seed else ""
        cl.login(username, password, verification_code=code)
    except TwoFactorRequired:
        sms_code = input("Two-factor code (from SMS or app): ").strip()
        cl.login(username, password, verification_code=sms_code)
    except Exception as e:
        print(f"\n✗ Login failed: {e}")
        return 1

    uid = str(cl.user_id or "")
    if EXPECTED_USER_ID and uid != EXPECTED_USER_ID:
        print(f"\n⚠  Logged into account user_id={uid}, expected {EXPECTED_USER_ID}.")
        if input("   Store this account anyway? [y/N] ").strip().lower() != "y":
            print("Aborted — nothing stored.")
            return 1

    # Persist credentials (Keychain) and the session (file).
    keyring.set_password(KEYRING_SERVICE, "username", username)
    keyring.set_password(KEYRING_SERVICE, username, password)
    if totp_seed:
        keyring.set_password(KEYRING_SERVICE, f"totp:{username}", totp_seed)
    cl.dump_settings(SESSION_FILE)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass

    print(
        f"\n✓ Done. Credentials stored in the macOS Keychain (service "
        f"'{KEYRING_SERVICE}'),\n  session saved to {SESSION_FILE.name}, "
        f"logged in as user_id={uid}.\n"
        "  sync.py will now authenticate and re-login on its own.\n\n"
        "  Tip: on the first run the Keychain may ask permission — choose\n"
        "  \"Always Allow\" so the scheduled task runs without a prompt."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
