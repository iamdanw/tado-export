"""OAuth2 device code flow against login.tado.com.

tado removed the password grant in March 2025. The only supported way to get a
token for a personal script is the device code flow: we ask for a device code,
the user approves it in a browser, then we poll until a token is issued.

Refresh tokens rotate on every use and are valid for up to 30 days, so the new
one must be persisted immediately or the link is lost.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import (
    CLIENT_ID,
    DEFAULT_PROFILE,
    DEVICE_AUTHORIZE_URL,
    SCOPE,
    TOKEN_REFRESH_MARGIN_S,
    TOKEN_URL,
    USER_AGENT,
    token_path,
)


class AuthError(RuntimeError):
    """Raised when the user is not linked, or the link has expired."""


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float  # unix seconds

    @property
    def stale(self) -> bool:
        return time.time() >= self.expires_at - TOKEN_REFRESH_MARGIN_S


class TokenStore:
    """Persists one account's rotating refresh token to a 0600 file."""

    def __init__(self, path: Path | None = None, profile: str = DEFAULT_PROFILE) -> None:
        self.profile = profile
        self.path = Path(path) if path else token_path(profile)

    def load(self) -> Token | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text())
            return Token(
                access_token=raw.get("access_token", ""),
                refresh_token=raw["refresh_token"],
                expires_at=float(raw.get("expires_at", 0)),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise AuthError(f"Token file {self.path} is corrupt: {exc}") from exc

    def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "access_token": token.access_token,
                    "refresh_token": token.refresh_token,
                    "expires_at": token.expires_at,
                },
                indent=2,
            )
        )
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        tmp.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def _token_request(session: requests.Session, payload: dict) -> requests.Response:
    return session.post(
        TOKEN_URL,
        data=payload,
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )


def _to_token(payload: dict) -> Token:
    return Token(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        expires_at=time.time() + float(payload.get("expires_in", 600)),
    )


def device_login(store: TokenStore, on_prompt=print) -> Token:
    """Run the full device code flow, blocking until the user approves."""
    session = requests.Session()
    resp = session.post(
        DEVICE_AUTHORIZE_URL,
        data={"client_id": CLIENT_ID, "scope": SCOPE},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    auth = resp.json()

    verification_url = auth.get("verification_uri_complete") or auth["verification_uri"]
    on_prompt("")
    on_prompt("Open this URL in a browser and approve access:")
    on_prompt(f"\n    {verification_url}\n")
    if "user_code" in auth:
        on_prompt(f"If prompted for a code, enter: {auth['user_code']}")
    on_prompt("Waiting for approval...")

    interval = int(auth.get("interval", 5))
    deadline = time.time() + int(auth.get("expires_in", 300))
    device_code = auth["device_code"]

    while time.time() < deadline:
        time.sleep(interval)
        poll = _token_request(
            session,
            {
                "client_id": CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if poll.status_code == 200:
            token = _to_token(poll.json())
            store.save(token)
            on_prompt("Approved. Refresh token stored at " + str(store.path))
            return token

        error = ""
        try:
            error = poll.json().get("error", "")
        except ValueError:
            pass
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "expired_token":
            raise AuthError("The approval window expired. Run `tado-export login` again.")
        if error == "access_denied":
            raise AuthError("Access was denied in the browser.")
        raise AuthError(f"Device flow failed ({poll.status_code}): {poll.text[:300]}")

    raise AuthError("Timed out waiting for browser approval.")


class TokenManager:
    """Thread-safe access token supplier, shared by all workers."""

    def __init__(self, store: TokenStore | None = None, profile: str = DEFAULT_PROFILE) -> None:
        self.store = store or TokenStore(profile=profile)
        self._lock = threading.Lock()
        self._token: Token | None = None
        self._session = requests.Session()

    def access_token(self) -> str:
        with self._lock:
            if self._token is None:
                self._token = self.store.load()
                if self._token is None:
                    hint = "" if self.store.profile == DEFAULT_PROFILE else f" --profile {self.store.profile}"
                    raise AuthError(
                        f"Not linked to tado yet. Run `tado-export login{hint}` first."
                    )
            if self._token.stale:
                self._token = self._refresh(self._token)
            return self._token.access_token

    def _refresh(self, token: Token) -> Token:
        resp = _token_request(
            self._session,
            {
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
            },
        )
        if resp.status_code != 200:
            raise AuthError(
                "Could not refresh the tado token "
                f"({resp.status_code}: {resp.text[:200]}). "
                "Refresh tokens expire after 30 days of disuse — "
                "run `tado-export login` to re-link."
            )
        new = _to_token(resp.json())
        # Rotation: the old refresh token is dead the moment this succeeds.
        self.store.save(new)
        return new
