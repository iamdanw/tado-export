"""Thin client for the my.tado.com v2 REST API.

Endpoint shapes follow the community OpenAPI spec at
https://github.com/kritsel/tado-openapispec-v2
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import date

import requests

from .auth import TokenManager
from .config import API_BASE, EIQ_BASE, HOPS_BASE, MINDER_BASE, USER_AGENT

# tado reports remaining quota as:  ratelimit: "perday";r=19873
# and, once exhausted:              ratelimit: "perday";r=0;t=3600
_RATELIMIT_RE = re.compile(r"r=(\d+)(?:\s*;\s*t=(\d+))?")


class TadoApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimitExceeded(TadoApiError):
    def __init__(self, retry_after_s: int | None) -> None:
        wait = f" Quota refills in ~{retry_after_s}s." if retry_after_s else ""
        super().__init__(f"tado daily API quota exhausted.{wait}", status=429)
        self.retry_after_s = retry_after_s


@dataclass
class RateLimit:
    remaining: int | None = None
    resets_in_s: int | None = None


class TadoClient:
    """Handles auth, retries and quota accounting. Safe to share across threads."""

    def __init__(
        self,
        tokens: TokenManager | None = None,
        *,
        min_interval_s: float = 0.0,
        max_retries: int = 4,
    ) -> None:
        self.tokens = tokens or TokenManager()
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.calls_made = 0
        self.rate_limit = RateLimit()
        self._lock = threading.Lock()
        self._next_call_at = 0.0
        self._local = threading.local()

    # -- plumbing ---------------------------------------------------------

    @property
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_call_at - now
            self._next_call_at = max(now, self._next_call_at) + self.min_interval_s
        if wait > 0:
            time.sleep(wait)

    def _note_rate_limit(self, response: requests.Response) -> None:
        header = response.headers.get("ratelimit") or response.headers.get("RateLimit")
        if not header:
            return
        match = _RATELIMIT_RE.search(header)
        if not match:
            return
        remaining = int(match.group(1))
        resets_in = int(match.group(2)) if match.group(2) else None
        with self._lock:
            self.rate_limit = RateLimit(remaining=remaining, resets_in_s=resets_in)

    def get(
        self,
        path: str,
        params: dict | None = None,
        *,
        allow_404: bool = False,
        base: str | None = None,
    ):
        """GET an API path. Returns parsed JSON, or None for a tolerated 404."""
        url = f"{base or API_BASE}{path}"
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            self._throttle()
            token = self.tokens.access_token()
            try:
                response = self._session.get(
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=45,
                )
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2**attempt)
                continue

            with self._lock:
                self.calls_made += 1
            self._note_rate_limit(response)

            if response.status_code == 200:
                return response.json()
            if response.status_code == 404 and allow_404:
                return None
            if response.status_code == 429:
                retry_after = self.rate_limit.resets_in_s
                header = response.headers.get("Retry-After")
                if header and header.isdigit():
                    retry_after = int(header)
                raise RateLimitExceeded(retry_after)
            if response.status_code in (401, 403):
                raise TadoApiError(
                    f"tado rejected the request ({response.status_code}) for {path}. "
                    "The token may have been revoked — try `tado-export login`.",
                    status=response.status_code,
                )
            if response.status_code >= 500:
                last_error = TadoApiError(
                    f"tado server error {response.status_code} for {path}",
                    status=response.status_code,
                )
                time.sleep(2**attempt)
                continue

            raise TadoApiError(
                f"GET {path} failed ({response.status_code}): {response.text[:300]}",
                status=response.status_code,
            )

        raise TadoApiError(f"GET {path} failed after {self.max_retries} attempts: {last_error}")

    # -- endpoints --------------------------------------------------------

    def me(self) -> dict:
        return self.get("/me")

    def home(self, home_id: int) -> dict:
        return self.get(f"/homes/{home_id}")

    def zones(self, home_id: int) -> list[dict]:
        return self.get(f"/homes/{home_id}/zones")

    def rooms_and_devices(self, home_id: int) -> dict | None:
        """tado X room + device inventory, from the newer hops.tado.com host.

        tado X (``generation: "LINE_X"``) homes have no zones — rooms took
        their place — and this is where they live instead. A room id can be
        used anywhere a zoneId is expected, ``dayReport`` included.
        """
        return self.get(f"/homes/{home_id}/roomsAndDevices", base=HOPS_BASE, allow_404=True)

    def running_times(self, home_id: int, start: date, end: date) -> dict | None:
        """Per-day, per-zone heating running time.

        Unlike dayReport this reaches back to when the home was created, and the
        whole history comes back in one call.
        """
        return self.get(
            f"/homes/{home_id}/runningTimes",
            params={
                "from": start.isoformat(),
                "to": end.isoformat(),
                "aggregate": "day",
                "summary_only": "false",
            },
            allow_404=True,
            base=MINDER_BASE,
        )

    def eiq_consumption(self, home_id: int, month: str, country: str) -> dict | None:
        """Energy IQ consumption for one month (``month`` as YYYY-MM)."""
        return self.get(
            f"/homes/{home_id}/consumptionOverview",
            params={"month": month, "country": country},
            allow_404=True,
            base=EIQ_BASE,
        )

    def eiq_meter_readings(self, home_id: int) -> dict | None:
        return self.get(f"/homes/{home_id}/meterReadings", allow_404=True, base=EIQ_BASE)

    def eiq_tariffs(self, home_id: int) -> list | dict | None:
        return self.get(f"/homes/{home_id}/tariffs", allow_404=True, base=EIQ_BASE)

    def day_report(self, home_id: int, zone_id: int, day: date) -> dict | None:
        """One zone-day of historic measurements. This is the workhorse call."""
        return self.get(
            f"/homes/{home_id}/zones/{zone_id}/dayReport",
            params={"date": day.isoformat()},
            allow_404=True,
        )
