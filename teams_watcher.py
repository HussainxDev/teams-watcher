"""
Teams Message Watcher — calls your phone when your manager messages you.

Monitors Windows toast notifications from Microsoft Teams.
No Azure AD or Microsoft account sign-in needed.

Prerequisites:
  pip install -r requirements.txt

Setup:
  1. Create a Twilio account at twilio.com and get:
     - Account SID, Auth Token, a Twilio phone number
  2. Copy .env.example to .env and fill in your values.
  3. Make sure Teams desktop is running with notifications enabled.
"""

import os
import sys
import time
import shutil
import sqlite3
import tempfile
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

from twilio.rest import Client as TwilioClient

# ── Config ───────────────────────────────────────────────────────────────────
MANAGER_NAME = os.environ.get("MANAGER_NAME", "")
MANAGER_EMAIL = os.environ.get("MANAGER_EMAIL", "")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
MY_PHONE_NUMBER = os.environ.get("MY_PHONE_NUMBER", "")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
CALL_COOLDOWN = int(os.environ.get("CALL_COOLDOWN", "120"))

# Windows notification database
NOTIFICATION_DB = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("teams-watcher")


# ── Notification DB helpers ──────────────────────────────────────────────────

def _safe_copy_db() -> Path:
    """Copy notification DB + WAL/SHM files to temp for safe reading."""
    temp_dir = Path(tempfile.gettempdir()) / "teams_watcher"
    temp_dir.mkdir(exist_ok=True)
    dest = temp_dir / "wpndatabase.db"
    shutil.copy2(NOTIFICATION_DB, dest)
    for suffix in ("-wal", "-shm"):
        src = NOTIFICATION_DB.with_name(NOTIFICATION_DB.name + suffix)
        if src.exists():
            shutil.copy2(src, dest.with_name(dest.name + suffix))
    return dest


def _find_teams_handler_ids(cursor: sqlite3.Cursor) -> list:
    """Return handler IDs that belong to Microsoft Teams."""
    cursor.execute("SELECT RecordId, PrimaryId FROM NotificationHandler")
    return [
        row[0] for row in cursor.fetchall()
        if "teams" in (row[1] or "").lower()
    ]


def get_new_notifications(since_order: int = 0) -> tuple:
    """
    Read new Teams toast notifications from the Windows notification DB.
    Returns (list_of_notifications, max_order_seen).
    """
    db_path = _safe_copy_db()
    results = []
    max_order = since_order

    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        handler_ids = _find_teams_handler_ids(cur)
        if not handler_ids:
            conn.close()
            return results, max_order

        placeholders = ",".join("?" for _ in handler_ids)
        cur.execute(
            f"SELECT [Order], HandlerId, Type, Payload FROM Notification "
            f"WHERE HandlerId IN ({placeholders}) AND [Order] > ? "
            f"ORDER BY [Order] ASC",
            [*handler_ids, since_order],
        )

        for row in cur.fetchall():
            order = row["Order"]
            max_order = max(max_order, order)
            payload = row["Payload"]
            if payload:
                results.append({"order": order, "payload": payload})

        conn.close()
    except (sqlite3.Error, OSError) as exc:
        log.warning("Could not read notification DB: %s", exc)
    finally:
        for suffix in ("", "-wal", "-shm"):
            p = db_path.with_name(db_path.name + suffix)
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    return results, max_order


def extract_texts(payload) -> list:
    """Pull all <text> values from a toast notification XML payload."""
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        root = ET.fromstring(payload)
        return [t.text.strip() for t in root.iter("text") if t.text]
    except ET.ParseError:
        return []


def matches_manager(texts: list) -> bool:
    """Return True if any text element matches the manager's name or email."""
    terms = []
    if MANAGER_NAME:
        terms.append(MANAGER_NAME.strip().lower())
    if MANAGER_EMAIL:
        terms.append(MANAGER_EMAIL.strip().lower())
    if not terms:
        return False
    blob = " ".join(texts).lower()
    return any(t in blob for t in terms)


# ── Twilio ───────────────────────────────────────────────────────────────────
def call_phone():
    """Place a phone call via Twilio with a short TTS message."""
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    call = client.calls.create(
        to=MY_PHONE_NUMBER,
        from_=TWILIO_FROM_NUMBER,
        twiml=(
            "<Response>"
            "<Say voice='alice'>Your manager sent you a message on Teams.</Say>"
            "<Pause length='1'/>"
            "<Say voice='alice'>Check Teams now.</Say>"
            "</Response>"
        ),
    )
    log.info("Call initiated — SID: %s", call.sid)


# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    if sys.platform != "win32":
        log.error("This tool only works on Windows (monitors Windows notifications).")
        sys.exit(1)

    # Validate config
    required = {
        "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
        "TWILIO_FROM_NUMBER": TWILIO_FROM_NUMBER,
        "MY_PHONE_NUMBER": MY_PHONE_NUMBER,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        log.error("Missing required .env values: %s", ", ".join(missing))
        sys.exit(1)

    if not MANAGER_NAME and not MANAGER_EMAIL:
        log.error("Set at least MANAGER_NAME or MANAGER_EMAIL in .env")
        sys.exit(1)

    if not NOTIFICATION_DB.exists():
        log.error("Notification database not found: %s", NOTIFICATION_DB)
        log.error("Make sure you are running Windows 10 or later.")
        sys.exit(1)

    # Baseline: note current max so we only alert on NEW notifications
    _, last_order = get_new_notifications(since_order=0)
    last_call_time = 0.0

    log.info("Teams Watcher started (Windows notification mode)")
    log.info("Watching for: %s", MANAGER_NAME or MANAGER_EMAIL)
    log.info("Will call: %s", MY_PHONE_NUMBER)
    log.info("Poll: %ds | Cooldown: %ds", POLL_INTERVAL, CALL_COOLDOWN)
    log.info("Keep Teams desktop running with notifications ON.")
    print()

    try:
        while True:
            time.sleep(POLL_INTERVAL)
            try:
                notifications, new_max = get_new_notifications(since_order=last_order)
                if new_max > last_order:
                    last_order = new_max

                for n in notifications:
                    texts = extract_texts(n["payload"])
                    if not matches_manager(texts):
                        continue

                    preview = " | ".join(texts)[:120]
                    log.info("Manager message: %s", preview)

                    now = time.time()
                    if now - last_call_time >= CALL_COOLDOWN:
                        call_phone()
                        last_call_time = now
                    else:
                        wait = int(CALL_COOLDOWN - (now - last_call_time))
                        log.info("Cooldown active (%ds left) — skipping call", wait)
            except Exception as exc:
                log.warning("Poll error: %s", exc)
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
