"""Financial Modeling Prep (FMP) client — the /stable API.

This key tier serves ONLY the /stable endpoints; legacy /api/v3 and /api/v4
return 403. All calls go through `fmp_get`, which caches and fails loudly when
the key is missing or the API errors, consistent with the rest of the server.
"""

from __future__ import annotations

import os

import requests

from .common import _cached

_BASE = "https://financialmodelingprep.com/stable"


class FMPError(Exception):
    """Raised when FMP is unusable (missing key, HTTP error, bad payload)."""


def api_key() -> str | None:
    return os.environ.get("FMP_API_KEY")


def have_key() -> bool:
    return bool(api_key())


def fmp_get(path: str, **params) -> list | dict:
    """GET an FMP /stable endpoint, cached for 60s. Raises FMPError on failure.

    `path` is the endpoint name (e.g. "ratios"). `params` are query params
    excluding the API key, which is injected here.
    """
    key = api_key()
    if not key:
        raise FMPError(
            "FMP_API_KEY is not set — fundamentals/valuation tools are "
            "unavailable. Add it to the MCP client config's env block."
        )

    cache_key = ("fmp", path, tuple(sorted(params.items())))

    def produce():
        query = {**params, "apikey": key}
        resp = requests.get(f"{_BASE}/{path}", params=query, timeout=20)
        if resp.status_code != 200:
            raise FMPError(f"FMP {path} HTTP {resp.status_code}: {resp.text[:160]}")
        data = resp.json()
        if isinstance(data, dict) and "Error Message" in data:
            raise FMPError(f"FMP {path}: {data['Error Message']}")
        return data

    return _cached(cache_key, produce)
