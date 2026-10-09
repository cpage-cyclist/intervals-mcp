"""Shared fixtures: a fake Intervals.icu API and a fully signed-in test client."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from intervals_mcp.client import IntervalsClient
from intervals_mcp.config import Settings
from intervals_mcp.server import build_app

PUBLIC = "https://intervals-mcp.example.com"
PASSWORD = "correct-horse-battery"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"

ATHLETE = {
    "id": "i1",
    "name": "Test Rider",
    "timezone": "Europe/London",
    "icu_weight": 70.0,
    "icu_resting_hr": 44,
    "wahoo_sync_activities": True,
    "wahoo_upload_workouts": True,
}
SPORT_SETTINGS = [
    {
        "types": ["Ride", "VirtualRide"],
        "ftp": 330,
        "indoor_ftp": 320,
        "w_prime": 22000,
        "lthr": 172,
        "max_hr": 192,
        "power_zones": [55, 75, 90, 105, 120, 150, 999],
        "hr_zones": [140, 155, 165, 172, 178, 185, 192],
        "sweet_spot_min": 84,
        "sweet_spot_max": 97,
    }
]
ACTIVITIES = [
    {
        "id": "i900",
        "start_date_local": "2026-10-08T09:00:00",
        "type": "Ride",
        "name": "3x12 Sweet spot",
        "description": "Legs good, last rep tough",
        "moving_time": 5400,
        "distance": 52000.0,
        "total_elevation_gain": 410.0,
        "icu_training_load": 98,
        "icu_intensity": 82.4,
        "icu_weighted_avg_watts": 272,
        "icu_average_watts": 241,
        "average_heartrate": 148.3,
        "icu_ftp": 330,
        "icu_rpe": 7,
        "trainer": False,
        "source": "WAHOO",
    },
    {"id": "i899", "start_date_local": "2026-10-06T08:00:00", "source": "STRAVA"},
]
ACTIVITY_DETAIL = {
    **ACTIVITIES[0],
    "icu_intervals": [
        {"label": "Rep 1", "type": "WORK", "start_time": 900, "moving_time": 720, "average_watts": 300,
         "weighted_average_watts": 302, "average_heartrate": 158, "intensity": 91, "zone": 3, "decoupling": 2.1},
        {"label": None, "type": "RECOVERY", "start_time": 1620, "moving_time": 240, "average_watts": 160},
    ],
    "icu_zone_times": [{"id": "Z1", "secs": 1200}, {"id": "Z2", "secs": 1500}, {"id": "Z3", "secs": 600},
                       {"id": "SS", "secs": 2160}],
    "icu_hr_zone_times": [600, 1800, 2400, 600, 0],
    "stream_types": ["time", "watts", "heartrate", "cadence"],
}
WELLNESS = [
    {"id": "2026-10-07", "ctl": 82.0, "atl": 95.5, "rampRate": 4.2, "restingHR": 45, "hrv": 71.0,
     "sleepSecs": 27000, "soreness": 2, "comments": "slept ok"},
    {"id": "2026-10-08", "ctl": 82.9, "atl": 98.0, "rampRate": 4.4, "weight": 70.2},
]
EVENTS = [
    {"id": 5001, "start_date_local": "2026-10-10T00:00:00", "category": "WORKOUT", "type": "Ride",
     "name": "VO2 5x4", "description": "- 15m ramp 50-75%\n\n5x\n- 4m 115%\n- 4m 50%\n\n- 10m 50%",
     "moving_time": 3900, "icu_training_load": 85, "indoor": True},
    {"id": 5002, "start_date_local": "2026-10-12T00:00:00", "category": "RACE_B", "type": "Ride",
     "name": "West Thames crit"},
]
FOLDERS = [
    {"id": 70, "name": "Threshold", "type": "FOLDER", "children": [
        {"id": 7001, "name": "2x20 FTP", "type": "Ride", "moving_time": 4200, "icu_training_load": 92,
         "description": "- 15m ramp 50-75%\n\n2x\n- 20m 95-100%\n- 5m 50%\n\n- 10m 50%", "folder_id": 70},
    ]},
]
POWER_CURVES = {"list": [
    {"id": "42d", "label": "42 days", "secs": [5, 15, 30, 60, 300, 1200, 3600],
     "values": [1210, 1050, 820, 560, 410, 352, 318],
     "watts_per_kg": [17.3, 15.0, 11.7, 8.0, 5.86, 5.03, 4.54]},
]}
STREAMS = [
    {"type": "time", "data": list(range(0, 60))},
    {"type": "watts", "data": [200] * 30 + [300] * 30},
    {"type": "heartrate", "data": [140] * 60},
]


class FakeIntervals:
    """Records every request; returns canned data shaped like the real API."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        p, m = request.url.path, request.method
        assert request.headers["authorization"].startswith("Basic ")
        routes: list[tuple[str, str, Any]] = [
            ("GET", r"/api/v1/athlete/i1$", ATHLETE),
            ("GET", r"/api/v1/athlete/i1/sport-settings$", SPORT_SETTINGS),
            ("GET", r"/api/v1/athlete/i1/activities$", ACTIVITIES),
            ("GET", r"/api/v1/activity/i900$", ACTIVITY_DETAIL),
            ("GET", r"/api/v1/activity/i900/messages$", [{"name": "Coach", "content": "Nice pacing"}]),
            ("GET", r"/api/v1/activity/i900/streams\.json$", STREAMS),
            ("GET", r"/api/v1/athlete/i1/wellness\.json$", WELLNESS),
            ("GET", r"/api/v1/athlete/i1/events\.json$", EVENTS),
            ("GET", r"/api/v1/athlete/i1/events/5001$", EVENTS[0]),
            ("GET", r"/api/v1/athlete/i1/folders$", FOLDERS),
            ("GET", r"/api/v1/athlete/i1/workouts/7001$", FOLDERS[0]["children"][0]),
            ("GET", r"/api/v1/athlete/i1/power-curves\.json$", POWER_CURVES),
        ]
        for method, pattern, payload in routes:
            if m == method and re.search(pattern, p):
                return httpx.Response(200, json=payload)
        if m in ("POST", "PUT"):
            body = json.loads(request.content or b"{}")
            base = EVENTS[0] if "/events" in p else FOLDERS[0]["children"][0] if "/workouts" in p else {}
            return httpx.Response(200, json={**base, **body, "id": base.get("id", 1)})
        if m == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(404, text="not found")


@pytest.fixture
def fake() -> FakeIntervals:
    return FakeIntervals()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        intervals_api_key="test-key",
        athlete_id="i1",
        connector_password=PASSWORD,
        token_secret="t" * 48,
        public_url=PUBLIC,
    )


@asynccontextmanager
async def running_app(settings: Settings, fake: FakeIntervals):
    client = IntervalsClient("test-key", "i1", transport=httpx.MockTransport(fake))
    app = build_app(settings, client)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as http:
            yield http


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def sign_in(http: httpx.AsyncClient, password: str = PASSWORD) -> dict[str, Any]:
    """Run the same OAuth flow Claude runs. Returns the token response (or the failed login page)."""
    reg = await http.post("/register", json={
        "client_name": "Claude", "redirect_uris": [CALLBACK],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    })
    assert reg.status_code == 201, reg.text
    client = reg.json()
    verifier, challenge = pkce()
    auth = await http.get("/authorize", params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
        "resource": f"{PUBLIC}/mcp",
    })
    assert auth.status_code in (302, 307), auth.text
    login_url = urlparse(auth.headers["location"])
    assert login_url.path == "/login"
    req = parse_qs(login_url.query)["req"][0]
    page = await http.get("/login", params={"req": req})
    assert page.status_code == 200 and "Connector password" in page.text
    done = await http.post("/login", data={"req": req, "password": password})
    if done.status_code != 302:
        return {"login_status": done.status_code, "login_page": done.text}
    cb = urlparse(done.headers["location"])
    q = parse_qs(cb.query)
    assert f"{cb.scheme}://{cb.netloc}{cb.path}" == CALLBACK
    assert q["state"] == ["xyz"]
    tok = await http.post("/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": CALLBACK,
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "code_verifier": verifier, "resource": f"{PUBLIC}/mcp",
    })
    assert tok.status_code == 200, tok.text
    return {**tok.json(), "client": client, "code": q["code"][0], "verifier": verifier}


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}


async def mcp_call(http: httpx.AsyncClient, token: str | None, method: str, params: dict | None = None,
                   msg_id: int = 1) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return await http.post("/mcp", headers=headers,
                           json={"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})


async def call_tool(http: httpx.AsyncClient, token: str, name: str, args: dict | None = None) -> Any:
    await mcp_call(http, token, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"}})
    r = await mcp_call(http, token, "tools/call", {"name": name, "arguments": args or {}}, msg_id=2)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        return {"error": text}
    return json.loads(text)
