"""Thin HTTP client for the simulator. Retries transport errors and 5xx; returns 4xx to the caller."""
from __future__ import annotations

import time

import httpx


class ApiError(Exception):
    def __init__(self, status: int, body):
        super().__init__(f"{status}: {body}")
        self.status = status
        self.body = body if isinstance(body, dict) else {"error": str(body)}

    @property
    def code(self) -> str:
        return self.body.get("error", "")


class SimClient:
    def __init__(self, base_url: str, retries: int = 6):
        self.http = httpx.Client(base_url=base_url, timeout=httpx.Timeout(300.0, connect=10.0))
        self.retries = retries

    def _req(self, method: str, path: str, raw: bool = False, **kw):
        delay = 0.5
        for attempt in range(self.retries + 1):
            try:
                r = self.http.request(method, path, **kw)
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 10)
                continue
            if r.status_code >= 500 and attempt < self.retries:
                time.sleep(delay)
                delay = min(delay * 2, 10)
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    body = r.text
                raise ApiError(r.status_code, body)
            return r.text if raw else r.json()

    def get(self, path, **params):
        return self._req("GET", path, params=params or None)

    def post(self, path, body):
        return self._req("POST", path, json=body)

    def put(self, path, body):
        return self._req("PUT", path, json=body)

    def chunk_text(self, n: int) -> str:
        return self._req("GET", f"/events/chunks/{n}", raw=True)
