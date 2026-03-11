"""
Teams Message Watcher — calls your phone when your manager messages you.

Prerequisites:
  pip install -r requirements.txt

Setup:
  1. Register an app in Azure AD (portal.azure.com -> App registrations):
     - Redirect URI: http://localhost
     - API Permissions: Chat.Read (delegated), User.Read (delegated)
     - Enable "Allow public client flows"
  2. Create a Twilio account at twilio.com and get:
     - Account SID, Auth Token, a Twilio phone number
  3. Copy .env.example to .env and fill in your values.
"""

import os
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

import msal
import requests
from twilio.rest import Client as TwilioClient

# ── Config ───────────────────────────────────────────────────────────────────
# Azure AD app registration
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID", "")

# Manager's display name or email (used to match incoming messages)
MANAGER_NAME = os.environ.get("MANAGER_NAME", "")
MANAGER_EMAIL = os.environ.get("MANAGER_EMAIL", "")

# Twilio
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
MY_PHONE_NUMBER = os.environ.get("MY_PHONE_NUMBER", "")

# How often to check for new messages (seconds)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "15"))

# Minimum seconds between phone calls (avoid spam-calling yourself)
CALL_COOLDOWN = int(os.environ.get("CALL_COOLDOWN", "120"))

# ── Validation ───────────────────────────────────────────────────────────────
_REQUIRED = {
    "AZURE_CLIENT_ID": AZURE_CLIENT_ID,
    "AZURE_TENANT_ID": AZURE_TENANT_ID,
    "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
    "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
    "TWILIO_FROM_NUMBER": TWILIO_FROM_NUMBER,
    "MY_PHONE_NUMBER": MY_PHONE_NUMBER,
}

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("teams-watcher")


# ── Auth (device-code flow — no client secret needed) ────────────────────────
SCOPES = ["Chat.Read", "User.Read"]

def get_access_token() -> str:
    """Authenticate via interactive device-code flow and return an access token."""
    authority = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}"
    app = msal.PublicClientApplication(AZURE_CLIENT_ID, authority=authority)

    # Try cached token first
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]

    # Interactive device-code flow
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow.get('error_description', 'unknown error')}")

    print()
    print("=" * 54)
    print(f"  Open:  {flow['verification_uri']}")
    print(f"  Code:  {flow['user_code']}")
    print("=" * 54)
    print()

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description', 'unknown error')}")
    log.info("Authenticated successfully.")
    return result["access_token"]


# ── Graph helpers ────────────────────────────────────────────────────────────
GRAPH = "https://graph.microsoft.com/v1.0"

def graph_get(token: str, url: str):
    """Make a GET request to Microsoft Graph."""
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_chats(token: str):
    """Return the user's recent chats."""
    return graph_get(token, f"{GRAPH}/me/chats?$top=50")["value"]


def get_recent_messages(token: str, chat_id: str, since: str):
    """Return messages in a chat newer than `since` (ISO-8601)."""
    url = (
        f"{GRAPH}/me/chats/{chat_id}/messages"
        f"?$top=20&$orderby=createdDateTime desc"
        f"&$filter=createdDateTime gt {since}"
    )
    try:
        return graph_get(token, url).get("value", [])
    except requests.HTTPError:
        return []


def is_from_manager(message: dict) -> bool:
    """Check whether a message was sent by the manager."""
    sender = message.get("from", {})
    user = sender.get("user", {}) if sender else {}
    display_name = (user.get("displayName") or "").lower()
    email = (user.get("email") or user.get("userPrincipalName") or "").lower()
    return (
        (MANAGER_NAME and MANAGER_NAME.lower() in display_name)
        or (MANAGER_EMAIL and MANAGER_EMAIL.lower() == email)
    )


# ── Twilio ───────────────────────────────────────────────────────────────────
def call_phone():
    """Place a phone call via Twilio with a short TTS message."""
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    call = client.calls.create(
        to=MY_PHONE_NUMBER,
        from_=TWILIO_FROM_NUMBER,
        twiml=(
            "<Response>"
            "<Say voice='alice'>You have a new Teams message from your manager.</Say>"
            "<Pause length='2'/>"
            "<Say voice='alice'>Check Teams now.</Say>"
            "</Response>"
        ),
    )
    log.info("Phone call initiated  —  SID: %s", call.sid)


# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    # Check required config
    missing = [k for k, v in _REQUIRED.items() if not v]
    if missing:
        print("ERROR: The following required values are not set:")
        for m in missing:
            print(f"  - {m}")
        print("\nCopy .env.example to .env and fill in your values.")
        return

    if not MANAGER_NAME and not MANAGER_EMAIL:
        print("ERROR: Set at least one of MANAGER_NAME or MANAGER_EMAIL.")
        return

    token = get_access_token()
    last_check = datetime.now(timezone.utc).isoformat()
    last_call_time = 0.0

    log.info("Watching Teams messages every %ds.", POLL_INTERVAL)
    log.info("Manager: %s (%s)", MANAGER_NAME, MANAGER_EMAIL)
    log.info("Will call: %s", MY_PHONE_NUMBER)

    while True:
        time.sleep(POLL_INTERVAL)
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            chats = get_chats(token)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401:
                log.warning("Token expired — re-authenticating...")
                token = get_access_token()
                continue
            raise

        manager_messaged = False
        for chat in chats:
            messages = get_recent_messages(token, chat["id"], last_check)
            for msg in messages:
                if is_from_manager(msg):
                    preview = (msg.get("body", {}).get("content") or "")[:80]
                    log.info("Manager message detected: %s", preview)
                    manager_messaged = True

        if manager_messaged:
            elapsed = time.time() - last_call_time
            if elapsed >= CALL_COOLDOWN:
                call_phone()
                last_call_time = time.time()
            else:
                log.info(
                    "Call cooldown active (%ds left) — skipping call.",
                    int(CALL_COOLDOWN - elapsed),
                )

        last_check = now_iso


if __name__ == "__main__":
    main()
