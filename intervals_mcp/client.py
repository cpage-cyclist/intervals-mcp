"""Thin async wrapper around the Intervals.icu REST API."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError


class IntervalsError(ToolError):
    """An error worth showing to Claude (and to you) in plain words.

    Subclassing ToolError means the message reaches Claude instead of a generic
    'Error executing tool'."""


_STATUS_HINTS = {
    401: "Intervals.icu rejected the API key. Check INTERVALS_API_KEY in the host settings.",
    403: "Intervals.icu refused access. Check INTERVALS_ATHLETE_ID matches the API key's account.",
    404: "Not found on Intervals.icu - check the id or date.",
    422: "Intervals.icu couldn't process that request",
    429: "Intervals.icu rate limit hit - wait a moment and try again.",
}


class IntervalsClient:
    def __init__(
        self,
        api_key: str,
        athlete_id: str,
        base_url: str = "https://intervals.icu/api/v1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.athlete_id = athlete_id
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=httpx.BasicAuth("API_KEY", api_key),
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"Accept": "application/json", "User-Agent": "intervals-mcp (personal)"},
            transport=transport,
        )

    @property
    def athlete_path(self) -> str:
        return f"/athlete/{self.athlete_id}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self._http.request(method, path, params=clean_params, json=json)
        except httpx.TimeoutException as exc:
            raise IntervalsError("Intervals.icu took too long to respond - try again.") from exc
        except httpx.HTTPError as exc:
            raise IntervalsError(f"Couldn't reach Intervals.icu: {exc}") from exc

        if resp.status_code >= 400:
            hint = _STATUS_HINTS.get(resp.status_code, f"Intervals.icu returned HTTP {resp.status_code}")
            detail = resp.text[:300].strip()
            raise IntervalsError(f"{hint}" + (f" (details: {detail})" if detail else ""))

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def aclose(self) -> None:
        await self._http.aclose()
