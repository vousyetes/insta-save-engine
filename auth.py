"""
auth.py — Authenticated Instagram client via instagrapi.

Why this exists: the web `sessionid` cookie expires every few weeks, which used
to break the pipeline silently. instagrapi logs in once, persists the session to
ig_session.json, and re-logs in automatically when the session dies — so the
sync is "set and forget".

Security:
  - Credentials live ONLY in the macOS Keychain (stored by setup_auth.py).
    They are never written to any file and never logged.
  - A persisted session (ig_session.json) means we rarely send a real login
    request, which keeps Instagram's risk checks calm.

It exposes get_ig_cookies(), which returns a cookie dict
({sessionid, csrftoken, ds_user_id, ...}) that the existing requests-based fetch
code in sync.py already knows how to use. So instagrapi only replaces the
"where do fresh cookies come from" part — nothing else in the pipeline changes.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import keyring
from instagrapi import Client
from instagrapi.exceptions import LoginRequired

log = logging.getLogger("ig-auth")

BASE_DIR        = Path(__file__).resolve().parent
SESSION_FILE    = BASE_DIR / "ig_session.json"
CONFIG_FILE     = BASE_DIR / "config.json"
KEYRING_SERVICE = "insta-save-engine"

# Optional safety net: lock the sync to one Instagram account by numeric user_id.
# Leave "instagram_expected_user_id" empty in config.json to disable the check
# (the normal case — you only log in with one account). Set it if you keep
# several accounts logged in and want to be 100% sure the engine never syncs
# the wrong one.
def _expected_user_id() -> str:
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        return str(cfg.get("instagram_expected_user_id", "") or "").strip()
    except Exception:
        return ""

EXPECTED_USER_ID = _expected_user_id()


# ── Keychain credentials ──────────────────────────────────────────────────────

def _load_credentials():
    """Return (username, password, totp_seed|None) from the macOS Keychain."""
    username = keyring.get_password(KEYRING_SERVICE, "username")
    if not username:
        raise RuntimeError(
            "No Instagram credentials in the Keychain. "
            "Run once:  .venv/bin/python setup_auth.py"
        )
    password = keyring.get_password(KEYRING_SERVICE, username)
    if not password:
        raise RuntimeError(
            f"No password stored for '{username}'. Re-run setup_auth.py."
        )
    totp_seed = keyring.get_password(KEYRING_SERVICE, f"totp:{username}")
    return username, password, totp_seed


# ── Client helpers ────────────────────────────────────────────────────────────

def _new_client() -> Client:
    cl = Client()
    cl.delay_range = [1, 3]  # small human-like delays between private calls
    return cl


def _dump_session(cl: Client) -> None:
    """Persist the session and keep it readable only by the owner (0600)."""
    cl.dump_settings(SESSION_FILE)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass


def _session_is_valid(cl: Client) -> bool:
    """Cheap authenticated probe using a low-risk endpoint.

    Deliberately NOT get_timeline_feed(): the main feed is one of the most
    risk-monitored endpoints and 403s even on healthy sessions, which used to
    trip a needless password login every run. account_info() is gentler.

    A genuine LoginRequired means the session is dead. Any other error is
    treated as transient (rate-limit / momentary 403): we retry once before
    giving up, so a passing hiccup does not burn a real login and alarm IG.
    """
    for attempt in range(2):
        try:
            cl.account_info()
            return True
        except LoginRequired:
            return False
        except Exception as e:
            if attempt == 0:
                log.info("Session probe hiccup (%s) — retrying once.", e)
                time.sleep(3)
                continue
            log.warning("Session probe failed twice (%s) — treating as expired.", e)
            return False
    return False


def _assert_right_account(cl: Client) -> None:
    uid = str(cl.user_id or "")
    if EXPECTED_USER_ID and uid and uid != EXPECTED_USER_ID:
        raise RuntimeError(
            f"Logged into the wrong account (user_id={uid}, "
            f"expected {EXPECTED_USER_ID}). Aborting to avoid syncing the wrong saves."
        )


# ── Public API ────────────────────────────────────────────────────────────────

def get_authenticated_client() -> Client:
    """
    Return a logged-in instagrapi Client, reusing the persisted session when
    possible and only performing a real login when the session has expired.
    """
    username, password, totp_seed = _load_credentials()
    cl = _new_client()

    # 1) Try to reuse the persisted session — no real login, no risk.
    #    Crucially we do NOT call cl.login(username, password) here. Sending a
    #    password login on every run is exactly what makes Instagram suspect an
    #    intruder and invalidate the session, which then forces another login
    #    next run (a vicious cycle). We just load the stored cookies/device and
    #    probe; a real login only happens if the session is genuinely dead.
    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
            if _session_is_valid(cl):
                _assert_right_account(cl)
                _dump_session(cl)
                log.info("Instagram session reused (no login).")
                return cl
            log.info("Stored session no longer valid — re-logging in.")
        except LoginRequired:
            log.info("Stored session no longer authorized — re-logging in.")
        except Exception as e:
            log.warning("Could not reuse stored session (%s) — re-logging in.", e)

    # 2) Fresh login, reusing the device UUIDs from the old session if we have
    #    them (a stable device fingerprint lowers Instagram's suspicion).
    cl_fresh = _new_client()
    try:
        if SESSION_FILE.exists():
            old = cl.get_settings()
            if old.get("uuids"):
                cl_fresh.set_uuids(old["uuids"])
    except Exception:
        pass

    code = cl_fresh.totp_generate_code(totp_seed) if totp_seed else ""
    cl_fresh.login(username, password, verification_code=code)
    if not _session_is_valid(cl_fresh):
        raise RuntimeError("Login succeeded but the new session did not verify.")
    _assert_right_account(cl_fresh)
    _dump_session(cl_fresh)
    log.info("Instagram fresh login OK.")
    return cl_fresh


def get_ig_cookies() -> dict:
    """
    Return a cookie dict compatible with sync.make_session():
    {sessionid, csrftoken, ds_user_id, ...}. Raises if authentication fails.
    """
    cl = get_authenticated_client()
    jar = dict(cl.private.cookies.get_dict())
    jar.setdefault("sessionid", cl.sessionid or "")
    jar.setdefault("ds_user_id", str(cl.user_id or ""))
    jar.setdefault("csrftoken", jar.get("csrftoken", ""))
    return jar
