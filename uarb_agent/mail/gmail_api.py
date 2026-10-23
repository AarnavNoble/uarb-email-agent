"""Send through the Gmail API over HTTPS, for hosts that block SMTP ports (DigitalOcean does on new accounts).

Same account, same raw bytes as the SMTP path: Gmail files the reply in Sent Mail and threads it by the
In-Reply-To/References headers, so the outbox and its Sent Mail reconciliation work unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send?uploadType=media"


class GmailApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Gmail API {status}: {body[:300]}")
        self.status = status

    @property
    def permanent(self) -> bool:
        # 4xx means the request itself is wrong and will fail again; 429 and 5xx are worth a retry.
        return 400 <= self.status < 500 and self.status != 429


class GmailApiTransport:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *, timeout: float = 120):
        self._creds = Credentials(
            None,
            refresh_token=refresh_token,
            token_uri=TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        self.timeout = timeout

    def send(self, raw: bytes, from_addr: str, to_addrs: Sequence[str]) -> None:
        # Gmail takes the sender and recipients from the message headers; the envelope arguments are for SMTP.
        resp = AuthorizedSession(self._creds).post(
            SEND_URL, data=raw, headers={"Content-Type": "message/rfc822"}, timeout=self.timeout
        )
        if resp.status_code >= 300:
            raise GmailApiError(resp.status_code, resp.text)
