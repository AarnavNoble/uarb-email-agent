"""One-time consent for Gmail API sending. Run on a machine with a browser, signed in as the agent account.

uv run python scripts/gmail_auth.py [client_secret.json]

Writes GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN into .env without printing them. The OAuth
app must be published "In production": refresh tokens of apps left in "Testing" expire after 7 days.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from dotenv import set_key
from google_auth_oauthlib.flow import InstalledAppFlow

from uarb_agent.mail.gmail_api import SCOPES

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    client_file = Path(argv[0]) if argv else ROOT / "client_secret.json"
    flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        print("Google returned no refresh token; revoke the app's access and run this again.", file=sys.stderr)
        return 1
    client = json.loads(client_file.read_text())["installed"]
    env = ROOT / ".env"
    set_key(env, "GMAIL_CLIENT_ID", client["client_id"], quote_mode="never")
    set_key(env, "GMAIL_CLIENT_SECRET", client["client_secret"], quote_mode="never")
    set_key(env, "GMAIL_REFRESH_TOKEN", creds.refresh_token, quote_mode="never")
    set_key(env, "GMAIL_TOKEN_ISSUED", dt.date.today().isoformat(), quote_mode="never")
    print(f"Saved Gmail API credentials to {env}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
