"""Each tool hits the right Intervals.icu endpoint and returns tidy, correct numbers."""

import json

from contextlib import asynccontextmanager
from conftest import call_tool, running_app, sign_in

from intervals_mcp.tools import downsample


@asynccontextmanager
async def signed_in(settings, fake):
    async with running_app(settings, fake) as http:
        tok = await sign_in(http)
        yield http, tok["access_token"], fake


async def test_profile(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, _ = session
        out = await call_tool(http, token, "get_athlete_profile")
        assert out["sports"][0]["ftp"] == 330
        assert out["sports"][0]["indoor_ftp"] == 320
        assert out["device_sync"]["wahoo"] == {"activities_in": True, "workouts_out": True}
        assert len(out["today"]) == 10


async def test_list_activities(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, fake = session
        out = await call_tool(http, token, "list_activities", {"oldest": "2026-10-01"})
        ride = out["activities"][0]
        assert ride["np_w"] == 272 and ride["load"] == 98 and ride["moving_min"] == 90.0
        assert ride["IF_pct"] == 82.4
        assert ride["notes"] == "Legs good, last rep tough"
        assert "Strava" in out["activities"][1]["note"]
        sent = fake.requests[-1].url.params
        assert sent["oldest"] == "2026-10-01" and "icu_weighted_avg_watts" in sent["fields"]


async def test_bad_date_is_explained(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, _ = session
        out = await call_tool(http, token, "list_activities", {"oldest": "last week"})
        assert "YYYY-MM-DD" in out["error"] or "2026-10-09" in out["error"]


async def test_activity_detail(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, _ = session
        out = await call_tool(http, token, "get_activity", {"activity_id": "i900"})
        assert out["intervals"][0]["avg_w"] == 300
        assert out["intervals"][0]["decoupling_pct"] == 2.1
        assert out["time_in_power_zones_min"]["SS"] == 36.0
        assert out["comments"][0]["text"] == "Nice pacing"


async def test_streams_are_chart_ready(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, _ = session
        out = await call_tool(http, token, "get_activity_streams",
                              {"activity_id": "i900", "streams": ["watts", "heartrate", "cadence"], "resolution_s": 30})
        assert out["time_s"] == [0, 30]
        assert out["watts"] == [200, 300]
        assert out["not_recorded"] == ["cadence"]


def test_downsample_caps_points():
    t = list(range(10000))
    out = downsample(t, {"watts": [250] * 10000}, 1, 500)
    assert len(out["time_s"]) <= 501 and out["step_s"] >= 20


async def test_fitness_form_is_ctl_minus_atl(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, _ = session
        out = await call_tool(http, token, "get_fitness_and_wellness", {"oldest": "2026-10-07"})
        d = out["days"][0]
        assert d["form_tsb"] == -13.5 and d["sleep_h"] == 7.5 and d["hrv_rmssd"] == 71.0


async def test_power_curve_picks_durations(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, fake = session
        out = await call_tool(http, token, "get_power_curve", {"curves": ["42d"]})
        best = out[0]["best"]
        assert best["20m"] == {"w": 352, "wkg": 5.03}
        assert best["5m"]["w"] == 410
        assert "2h" not in best or best["2h"]["w"] == 318  # nearest shorter duration fallback
        assert fake.requests[-1].url.params["type"] == "Ride"


async def test_calendar_and_library(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, _ = session
        cal = await call_tool(http, token, "get_calendar", {"oldest": "2026-10-09", "newest": "2026-10-16"})
        assert cal[0]["id"] == 5001 and "5x" in cal[0]["description"]
        lib = await call_tool(http, token, "list_workout_library")
        assert lib[0]["workouts"][0]["name"] == "2x20 FTP"
        w = await call_tool(http, token, "get_library_workout", {"workout_id": 7001})
        assert "20m 95-100%" in w["description"]


async def test_create_planned_workout_sends_correct_body(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, fake = session
        out = await call_tool(http, token, "create_planned_workout", {
            "workout_date": "2026-10-14", "name": "Sweet spot 3x15",
            "description": "- 15m ramp 50-75%\n\n3x\n- 15m 90%\n- 5m 50%", "indoor": True})
        req = fake.requests[-1]
        body = json.loads(req.content)
        assert req.method == "POST" and req.url.path == "/api/v1/athlete/i1/events"
        assert body["start_date_local"] == "2026-10-14T00:00:00"
        assert body["category"] == "WORKOUT" and body["type"] == "Ride" and body["indoor"] is True
        assert out["created"]["name"] == "Sweet spot 3x15"


async def test_create_from_library(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, fake = session
        await call_tool(http, token, "create_planned_workout", {
            "workout_date": "2026-10-15", "name": "2x20", "from_library_workout_id": 7001})
        body = json.loads(fake.requests[-1].content)
        assert "20m 95-100%" in body["description"]


async def test_update_library_workout_only_sends_changes(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, fake = session
        out = await call_tool(http, token, "update_library_workout", {
            "workout_id": 7001, "description": "- 15m ramp 50-75%\n\n2x\n- 22m 95%\n- 5m 50%"})
        req = fake.requests[-1]
        assert req.method == "PUT" and req.url.path == "/api/v1/athlete/i1/workouts/7001"
        assert json.loads(req.content) == {"description": "- 15m ramp 50-75%\n\n2x\n- 22m 95%\n- 5m 50%"}
        assert "20m 95-100%" in out["before"]["description"]
        assert "22m 95%" in out["after"]["description"]


async def test_update_with_nothing_to_change_is_refused(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, fake = session
        n = len(fake.requests)
        out = await call_tool(http, token, "update_planned_workout", {"event_id": 5001})
        assert "Nothing to change" in out["error"]
        assert len(fake.requests) == n  # never touched the API


async def test_delete_event(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, fake = session
        out = await call_tool(http, token, "delete_calendar_event", {"event_id": 5001})
        assert fake.requests[-1].method == "DELETE"
        assert out["deleted"]["name"] == "VO2 5x4"


async def test_log_wellness(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, fake = session
        await call_tool(http, token, "log_wellness", {"day": "2026-10-09", "weight_kg": 70.4, "sleep_hours": 8})
        body = json.loads(fake.requests[-1].content)
        assert body == {"weight": 70.4, "sleepSecs": 28800}


async def test_write_tools_are_flagged_as_writes(settings, fake):
    async with signed_in(settings, fake) as session:
        http, token, _ = session
        from conftest import mcp_call
        r = await mcp_call(http, token, "tools/list", msg_id=3)
        tools = {t["name"]: t for t in r.json()["result"]["tools"]}
        assert tools["get_activity"]["annotations"]["readOnlyHint"] is True
        for name in ["create_planned_workout", "update_planned_workout", "delete_calendar_event",
                     "create_library_workout", "update_library_workout", "log_wellness", "update_activity_notes"]:
            assert tools[name]["annotations"]["readOnlyHint"] is False, name
