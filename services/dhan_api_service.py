"""Dhan API service implementation."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Mapping

IST = ZoneInfo("Asia/Kolkata")

import requests
from dhanhq import DhanContext, dhanhq

from config import AppConfig
from services.request_scheduler_service import KeyedRequestScheduler, build_request_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DhanCredentials:
    client_id: str
    access_token: str


class DhanApiService:
    """Minimal Dhan API client."""

    def __init__(
        self,
        credentials: DhanCredentials,
        *,
        base_url: str = "https://api.dhan.co/v2",
        timeout: float = 30.0,
        max_retries: int = 3,
        expiry_cache_path: Path | str | None = None,
        scheduler: KeyedRequestScheduler | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.credentials = credentials
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.expiry_cache_path = Path(expiry_cache_path) if expiry_cache_path is not None else None
        self.scheduler = scheduler or KeyedRequestScheduler()
        self.session = session or requests.Session()

    @classmethod
    def from_config(cls, config: AppConfig) -> "DhanApiService | None":
        client_id = os.getenv("CLIENT_ID")
        access_token = os.getenv("ACCESS_TOKEN")
        if not client_id or not access_token:
            return None
        return cls(
            DhanCredentials(client_id=client_id, access_token=access_token),
            base_url=os.getenv("DHAN_BASE_URL", "https://api.dhan.co/v2"),
            timeout=float(os.getenv("API_TIMEOUT", "30")),
            max_retries=int(os.getenv("MAX_RETRIES", "3")),
            expiry_cache_path=config.expiry_cache_path,
            scheduler=KeyedRequestScheduler(),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "access-token": self.credentials.access_token,
            "client-id": self.credentials.client_id,
            "Content-Type": "application/json",
        }

    def renew_token(self) -> str | None:
        """Call Dhan's RenewToken endpoint and return a fresh 24h access token.

        RenewToken uses the ``dhanClientId`` header (its own contract, distinct from the
        ``client-id`` the data endpoints use) and returns the new token under ``token``.
        Returns ``None`` on any failure (logged) — the caller decides what to do.
        """
        url = f"{self.base_url}/RenewToken"
        headers = {
            "access-token": self.credentials.access_token.strip(),
            "dhanClientId": str(self.credentials.client_id).strip(),
        }
        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException:
            logger.exception("RenewToken request failed")
            return None
        if response.status_code != 200:
            logger.error("RenewToken non-200 (%s): %s", response.status_code, response.text[:300])
            return None
        try:
            data = response.json()
        except json.JSONDecodeError:
            logger.exception("RenewToken response was not JSON")
            return None
        new_token = data.get("token") or data.get("accessToken")
        if not new_token:
            # Log only the response shape, never the body — it may carry the token under an
            # unexpected key (or other secrets), which must not land in logs/console.
            logger.error(
                "RenewToken response missing 'token'/'accessToken'; keys=%s",
                sorted(data) if isinstance(data, dict) else type(data).__name__,
            )
            return None
        return str(new_token)

    def _post(
        self,
        endpoint: str,
        payload: Mapping[str, Any],
        request_key: str | None = None,
    ) -> dict[str, Any] | None:
        url = f"{self.base_url}{endpoint}"
        for attempt in range(self.max_retries + 1):
            if request_key is not None:
                self.scheduler.wait_for_slot(request_key)
            try:
                response = self.session.post(
                    url,
                    headers=self._headers(),
                    json=dict(payload),
                    timeout=self.timeout,
                )
                if response.status_code == 429:
                    logger.warning("Dhan API rate limit encountered on %s", endpoint)
                    if request_key is not None:
                        self.scheduler.note_rate_limit()
                    if attempt < self.max_retries:
                        continue
                    return None

                response.raise_for_status()
                payload_json = response.json()
                if request_key is not None:
                    self.scheduler.record_result(request_key, success=True)
                return payload_json
            except (requests.ConnectionError, requests.Timeout) as exc:
                # Expected, self-healing transient network blips (host unreachable, timeout).
                # One-line WARNING, no stack — must precede the broad RequestException clause
                # below since both are RequestException subclasses (top-down except matching).
                logger.warning("Dhan API network error on %s (attempt %d): %s", endpoint, attempt + 1, exc)
                if request_key is not None:
                    self.scheduler.record_result(request_key, success=False)
                if attempt >= self.max_retries:
                    return None
            except (requests.RequestException, json.JSONDecodeError):
                logger.exception("Dhan API request failed for %s", endpoint)
                if request_key is not None:
                    self.scheduler.record_result(request_key, success=False)
                if attempt >= self.max_retries:
                    return None
        return None

    def _read_expiry_cache(self, asset_id: int, segment_code: str) -> list[str]:
        if self.expiry_cache_path is None or not self.expiry_cache_path.exists():
            return []
        try:
            cache_payload = json.loads(self.expiry_cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        entry = cache_payload.get(f"{asset_id}|{segment_code}", {})
        today = datetime.now(IST).strftime("%Y-%m-%d")
        return [str(item) for item in entry.get("expiry_list", []) if str(item) >= today]

    def _write_expiry_cache(self, asset_id: int, segment_code: str, expiry_list: list[str]) -> None:
        if self.expiry_cache_path is None:
            return
        self.expiry_cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {}
        if self.expiry_cache_path.exists():
            try:
                payload = json.loads(self.expiry_cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
        payload[f"{asset_id}|{segment_code}"] = {"expiry_list": list(expiry_list)}
        self.expiry_cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def get_expiry_list(self, asset_id: int, segment_code: str, use_cache: bool = True) -> list[str]:
        if use_cache:
            cached = self._read_expiry_cache(asset_id, segment_code)
            if cached:
                return cached

        request_key = build_request_key(asset_id, segment_code, None)
        payload = {"UnderlyingScrip": asset_id, "UnderlyingSeg": segment_code}
        response = self._post("/optionchain/expirylist", payload, request_key=request_key)
        if not response or response.get("status") != "success":
            return []
        expiry_list = [str(item) for item in response.get("data", [])]
        if expiry_list:
            self._write_expiry_cache(asset_id, segment_code, expiry_list)
        return expiry_list

    def fetch_option_chain(self, asset_id: int, segment_code: str, expiry: str) -> dict[str, Any] | None:
        request_key = build_request_key(asset_id, segment_code, expiry)
        payload = {
            "UnderlyingScrip": asset_id,
            "UnderlyingSeg": segment_code,
            "Expiry": expiry,
        }
        response = self._post("/optionchain", payload, request_key=request_key)
        if not response:
            return None
        return response.get("data")

    def get_vix_data(self) -> tuple[float, dict[str, Any]] | None:
        """Fetch India VIX using the DhanHQ client."""
        request_key = build_request_key(21, "IDX_I", "VIX")
        self.scheduler.wait_for_slot(request_key)
        try:
            dhan_context = DhanContext(self.credentials.client_id, self.credentials.access_token)
            dhan = dhanhq(dhan_context)
            result = dhan.quote_data({"IDX_I": [21]})
            if result.get("status") != "success":
                self.scheduler.record_result(request_key, success=False)
                return None

            vix_data = result.get("data", {}).get("data", {}).get("IDX_I", {}).get("21", {})
            if not vix_data:
                self.scheduler.record_result(request_key, success=False)
                return None

            value = float(vix_data.get("last_price") or vix_data.get("ohlc", {}).get("close") or 0)
            if value <= 0:
                self.scheduler.record_result(request_key, success=False)
                return None

            ohlc = vix_data.get("ohlc", {}) or {}
            metadata = {
                "previous_close": float(ohlc.get("close", 0) or 0),
                "day_high": float(ohlc.get("high", 0) or 0),
                "day_low": float(ohlc.get("low", 0) or 0),
                "net_change": float(vix_data.get("net_change", 0) or 0),
                "last_update_time": str(vix_data.get("last_trade_time", "")),
            }
            self.scheduler.record_result(request_key, success=True)
            return value, metadata
        except Exception:
            self.scheduler.record_result(request_key, success=False)
            logger.exception("Failed to fetch VIX data")
            return None
