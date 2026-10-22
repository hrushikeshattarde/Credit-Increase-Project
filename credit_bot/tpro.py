"""Thin client for the Transport Pro public API.

Auth: POST /auth with HTTP Basic -> bearer token (15 min) + refresh token.
The API answers a bare 404 to the default python-requests User-Agent, so a
custom one is always sent. Search endpoints page 200 records at a time
(0-based "page" query parameter) and load searches must carry a pickup /
delivery / billing date window of at most 60 days.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

MAX_WINDOW_DAYS = 60
PAGE_SIZE = 200


class TransportProError(RuntimeError):
    pass


class TransportProClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": "circle-credit-increase-bot/1.0", "Accept": "application/json"}
        )
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0

    # ------------------------------------------------------------------ auth
    def _login(self) -> None:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                resp = self._session.post(
                    f"{self.base_url}/auth",
                    auth=(self.username, self.password),
                    timeout=self.timeout,
                )
                if resp.status_code >= 500:
                    raise TransportProError(f"Transport Pro login failed: HTTP {resp.status_code}")
                self._store_token(resp, "login")
                return
            except (requests.RequestException, TransportProError) as exc:
                last_error = exc
                time.sleep(2 * (attempt + 1))
        raise TransportProError(f"Transport Pro login failed after retries: {last_error}")

    def _refresh(self) -> bool:
        if not self._refresh_token:
            return False
        try:
            resp = self._session.post(
                f"{self.base_url}/auth",
                json={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
                timeout=self.timeout,
            )
        except requests.RequestException:
            return False
        if not resp.ok:
            return False
        try:
            self._store_token(resp, "refresh")
            return True
        except TransportProError:
            return False

    def _store_token(self, resp: requests.Response, what: str) -> None:
        if not resp.ok:
            raise TransportProError(
                f"Transport Pro {what} failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        data = resp.json()
        token = data.get("access_token") or data.get("accessToken") or data.get("token")
        if not token:
            raise TransportProError(f"Transport Pro {what} returned no access token: {list(data)}")
        self._access_token = token
        self._refresh_token = data.get("refresh_token") or data.get("refreshToken")
        ttl = int(data.get("expires_in") or data.get("expiresIn") or 900)
        self._expires_at = time.time() + max(ttl - 60, 30)

    def _token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._access_token or time.time() >= self._expires_at:
                if force or not self._refresh():
                    self._login()
            assert self._access_token is not None
            return self._access_token

    # --------------------------------------------------------------- request
    def get(self, path: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        url = f"{self.base_url}{path}"
        last_error = "unknown error"
        need_new_token = False
        for attempt in range(4):
            headers = {"Authorization": f"Bearer {self._token(force=need_new_token)}"}
            need_new_token = False
            try:
                resp = self._session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 401:
                last_error = "HTTP 401"
                need_new_token = True
                continue
            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                time.sleep(2 * (attempt + 1))
                continue
            if not resp.ok:
                raise TransportProError(
                    f"GET {path} {params} -> HTTP {resp.status_code}: {resp.text[:300]}"
                )
            return resp.json()
        raise TransportProError(f"GET {path} failed after retries: {last_error}")

    def get_pages(self, path: str, **params: Any) -> Iterator[dict]:
        """Yield every record of a paginated endpoint ({pagination, results})."""
        page = 0
        while True:
            data = self.get(path, page=page, **params)
            results = data.get("results") or []
            yield from results
            pagination = data.get("pagination") or {}
            total_pages = int(pagination.get("totalPages") or 0)
            page += 1
            if page >= total_pages or not results:
                break

    # ------------------------------------------------------------- customers
    def customer(self, customer_id: int) -> dict:
        return self.get(f"/customer/{customer_id}")

    def customer_credit(self, customer_id: int) -> dict:
        return self.get(f"/customer/{customer_id}/credit")

    # ----------------------------------------------------------------- loads
    @staticmethod
    def windows(lookback_days: int, end: date | None = None) -> Iterator[tuple[date, date]]:
        """Split [end - lookback_days, end] into windows of at most 60 days, newest first."""
        end = end or date.today()
        start_limit = end - timedelta(days=lookback_days)
        while end >= start_limit:
            start = max(end - timedelta(days=MAX_WINDOW_DAYS - 1), start_limit)
            yield start, end
            end = start - timedelta(days=1)

    def search_loads(
        self,
        customer_id: int,
        lookback_days: int,
        load_status: str | None = None,
        billing_status: str | None = None,
        has_billing_lock: bool | None = None,
        future_days: int = 60,
    ) -> list[dict]:
        """All loads for a customer matching the status filters over the lookback.

        Windows are keyed on pickup date. future_days extends the newest window
        past today so loads with a future pickup date are not skipped.
        has_billing_lock=True keeps only loads on Billing Hold (the web
        screen's "Hold Enabled"; API parameter hasBillingLock).
        """
        seen: dict[int, dict] = {}
        end = date.today() + timedelta(days=future_days)
        for start, stop in self.windows(lookback_days + future_days, end=end):
            for load in self.get_pages(
                "/load/search",
                customerId=customer_id,
                loadStatus=load_status,
                billingStatus=billing_status,
                hasBillingLock="true" if has_billing_lock else None,
                pickupDateStart=start.isoformat(),
                pickupDateEnd=stop.isoformat(),
            ):
                seen[int(load["id"])] = load
        return list(seen.values())

    def load(self, load_id: int) -> dict:
        return self.get(f"/load/{load_id}")

    def missing_documents(self, workers: int = 8) -> dict[int, list[str]]:
        """Map of load id -> missing document names, across the whole tenant.

        The endpoint ignores every filter (customer, status, page size), so all
        pages are fetched in parallel and callers intersect with their own
        load set.
        """
        first = self.get("/load/missing_documents", page=0)
        pagination = first.get("pagination") or {}
        total_pages = int(pagination.get("totalPages") or 1)
        result: dict[int, list[str]] = {}

        def collect(data: dict) -> None:
            for rec in data.get("results") or []:
                result[int(rec["id"])] = list(rec.get("missingDocuments") or [])

        collect(first)
        if total_pages > 1:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                pages = pool.map(
                    lambda p: self.get("/load/missing_documents", page=p),
                    range(1, total_pages),
                )
                for data in pages:
                    collect(data)
        log.info("missing_documents: %s loads across %s pages", len(result), total_pages)
        return result

    def files_for_load(self, load_id: int) -> list[dict]:
        return list(self.get_pages("/files/search", recordType="loads", recordId=load_id))
