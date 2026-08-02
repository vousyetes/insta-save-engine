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
LOGIN_STAMP     = BASE_DIR / ".last_login"
KEYRING_SERVICE = "insta-save-engine"

# Every real password login fires a "new login" security alert on the account
# and pushes Instagram's risk engine one notch further towards a checkpoint. A
# broken session can otherwise turn into one login per scheduled run, which
# reads as an intrusion attempt and gets the account repeatedly logged out.
# These two cooldowns are the circuit breaker: after a real login we refuse to
# send another one for a while, and a challenge means Instagram is already
# unhappy, so we back off instead of hammering it at the next run.
LOGIN_COOLDOWN_H = 6

# Deliberately 20h and not 24h. The sync usually runs on a fixed daily
# schedule, so a 24h backoff started by run N is still ticking (by seconds to
# minutes) when run N+1 fires — the backoff silently eats a second day, every
# time. Anything under ~23h leaves the intended guarantee intact — at most one
# password login per day — while letting the next scheduled run actually retry.
CHALLENGE_COOLDOWN_H = 20

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


# ── Login circuit breaker ─────────────────────────────────────────────────────

def _read_stamp() -> tuple[float, float]:
    """Return (timestamp_of_last_real_login, cooldown_hours_it_asked_for)."""
    try:
        raw = LOGIN_STAMP.read_text().split()
        return float(raw[0]), float(raw[1])
    except Exception:
        return 0.0, 0.0


def _write_stamp(hours: float) -> None:
    try:
        LOGIN_STAMP.write_text(f"{time.time()} {hours}")
    except OSError:
        pass


def _assert_login_allowed() -> None:
    """Raise if we logged in too recently, rather than firing another alert."""
    last, hours = _read_stamp()
    if not last:
        return
    waited = (time.time() - last) / 3600
    if waited < hours:
        raise RuntimeError(
            f"Login cooldown active: last real login {waited:.1f}h ago, "
            f"waiting {hours:.0f}h before sending another one (avoids "
            f"spamming Instagram security alerts). Delete {LOGIN_STAMP.name} "
            f"to force a login now."
        )


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
    #    Gated by the cooldown: a dead session is not a reason to log in three
    #    times a day.
    _assert_login_allowed()

    cl_fresh = _new_client()
    try:
        if SESSION_FILE.exists():
            old = cl.get_settings()
            if old.get("uuids"):
                cl_fresh.set_uuids(old["uuids"])
    except Exception:
        pass

    code = cl_fresh.totp_generate_code(totp_seed) if totp_seed else ""
    _write_stamp(LOGIN_COOLDOWN_H)  # stamp BEFORE, so a crash still counts
    try:
        cl_fresh.login(username, password, verification_code=code)
    except Exception as e:
        # A checkpoint means Instagram already distrusts us. Retrying at the
        # next run would just add another alert, so stand down for a while.
        if "challenge" in str(e).lower() or "checkpoint" in str(e).lower():
            _write_stamp(CHALLENGE_COOLDOWN_H)
            log.warning(
                "Instagram raised a challenge — backing off %dh. Open the "
                "Instagram app and approve the login prompt.",
                CHALLENGE_COOLDOWN_H,
            )
        raise

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
