"""The tools Claude can call.

Design rules:
- Return compact JSON with only the fields that matter for training questions,
  so Claude spends its attention on your data rather than API noise.
- Series data (streams, fitness, power curve) comes back chart-ready so Claude
  can plot it directly.
- Anything that changes your Intervals.icu account is a separate, clearly
  named tool marked as a write, so Claude asks before using it.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Callable, Literal
from zoneinfo import ZoneInfo

from mcp.types import ToolAnnotations
from pydantic import Field

from .client import IntervalsClient, IntervalsError

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
UPDATE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)

WORKOUT_SYNTAX = """\
Intervals.icu workout text (goes in `description`; Intervals parses it into steps):
- Every step is a line starting with "- ": duration (or distance) then target.
    - 10m 55%            10 minutes at 55% FTP
    - 30s 120-130%       range of FTP
    - 5m 250w            absolute watts
    - 20m Z2             power zone
    - 15m 75% HR  /  - 10m Z2 HR  /  - 8m 95% LTHR   heart-rate targets
    - 10m ramp 50-75%    ramp
    - 4m 105% 95rpm      add cadence
    - Seated climb 6m 90%   free text before the duration becomes the step cue
  Durations: 30s, 5m, 1h, 1h30m, 1m30s. Distances: 2km, 500m.
- Repeats: a line ending in "Nx" (e.g. "Main set 3x" or just "3x"), followed by the steps
  to repeat, then a blank line to close the block.
- Plain lines without "- " are section headers/notes (e.g. "Warmup", "Cooldown").
Example:
    Warmup
    - 12m ramp 50-75%

    Main set 3x
    - 12m 88-94% 90rpm
    - 4m 55%

    Cooldown
    - 10m 50%
After creating/updating, check the returned moving_time and training_load: if they are
0 or missing, the text didn't parse - fix the syntax rather than telling the user it worked.
"""

# ----------------------------------------------------------------------------- helpers


def _round(x: Any, nd: int = 1) -> Any:
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return None
        return round(x, nd)
    return x


def _prune(d: dict[str, Any]) -> dict[str, Any]:
    """Drop None/empty values and round floats."""
    out = {}
    for k, v in d.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        out[k] = _round(v)
    return out


def _mins(seconds: Any) -> float | None:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return round(seconds / 60, 1)


def _km(metres: Any) -> float | None:
    if not isinstance(metres, (int, float)) or metres <= 0:
        return None
    return round(metres / 1000, 2)


def _dump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def _check_date(value: str, name: str) -> str:
    try:
        date.fromisoformat(value[:10])
    except ValueError as exc:
        raise IntervalsError(f"{name} must be a date like 2026-10-09 (got {value!r})") from exc
    return value[:10]


def _truncate(text: Any, limit: int) -> Any:
    if isinstance(text, str) and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


# ----------------------------------------------------------------------------- shapers

ACTIVITY_LIST_FIELDS = [
    "id", "start_date_local", "type", "name", "description", "moving_time", "distance",
    "total_elevation_gain", "icu_training_load", "icu_intensity", "icu_weighted_avg_watts",
    "icu_average_watts", "average_heartrate", "max_heartrate", "average_cadence", "icu_ftp",
    "trainer", "race", "icu_rpe", "feel", "tags", "decoupling", "icu_efficiency_factor",
    "icu_variability_index", "source", "calories", "carbs_ingested", "paired_event_id",
]


def shape_activity_summary(a: dict[str, Any], description_limit: int = 300) -> dict[str, Any]:
    if a.get("source") == "STRAVA" and not a.get("moving_time"):
        return {
            "id": a.get("id"),
            "start_date_local": a.get("start_date_local"),
            "note": "Strava-only activity: Strava doesn't let Intervals share this via the API. "
            "Sync your Wahoo/Garmin directly to Intervals.icu to see it.",
        }
    return _prune(
        {
            "id": a.get("id"),
            "date": a.get("start_date_local"),
            "type": a.get("type"),
            "name": a.get("name"),
            "notes": _truncate(a.get("description"), description_limit),
            "moving_min": _mins(a.get("moving_time")),
            "km": _km(a.get("distance")),
            "elev_m": a.get("total_elevation_gain"),
            "load": a.get("icu_training_load"),
            "IF_pct": a.get("icu_intensity"),
            "np_w": a.get("icu_weighted_avg_watts"),
            "avg_w": a.get("icu_average_watts"),
            "avg_hr": a.get("average_heartrate"),
            "max_hr": a.get("max_heartrate"),
            "avg_cad": a.get("average_cadence"),
            "ftp_at_time": a.get("icu_ftp"),
            "VI": a.get("icu_variability_index"),
            "EF": a.get("icu_efficiency_factor"),
            "decoupling_pct": a.get("decoupling"),
            "rpe": a.get("icu_rpe"),
            "feel": a.get("feel"),
            "indoor": True if a.get("trainer") else None,
            "race": True if a.get("race") else None,
            "tags": a.get("tags"),
            "kcal": a.get("calories"),
            "carbs_g": a.get("carbs_ingested"),
            "planned_event_id": a.get("paired_event_id"),
        }
    )


def shape_interval(i: dict[str, Any]) -> dict[str, Any]:
    return _prune(
        {
            "label": i.get("label"),
            "type": i.get("type"),
            "start_s": i.get("start_time"),
            "dur_s": i.get("moving_time") or i.get("elapsed_time"),
            "avg_w": i.get("average_watts"),
            "np_w": i.get("weighted_average_watts"),
            "max_w": i.get("max_watts"),
            "wkg": i.get("average_watts_kg"),
            "intensity_pct": i.get("intensity"),
            "zone": i.get("zone"),
            "avg_hr": i.get("average_heartrate"),
            "max_hr": i.get("max_heartrate"),
            "avg_cad": i.get("average_cadence"),
            "load": i.get("training_load"),
            "decoupling_pct": i.get("decoupling"),
            "wbal_end_j": i.get("wbal_end"),
            "km": _km(i.get("distance")),
            "gradient_pct": i.get("average_gradient"),
        }
    )


def shape_event(e: dict[str, Any], description_limit: int | None = None) -> dict[str, Any]:
    desc = e.get("description")
    if description_limit:
        desc = _truncate(desc, description_limit)
    return _prune(
        {
            "id": e.get("id"),
            "date": (e.get("start_date_local") or "")[:10] or None,
            "category": e.get("category"),
            "type": e.get("type"),
            "name": e.get("name"),
            "description": desc,
            "planned_min": _mins(e.get("moving_time")),
            "planned_load": e.get("icu_training_load"),
            "IF_pct": e.get("icu_intensity"),
            "indoor": e.get("indoor"),
            "target": e.get("target"),
            "carbs_per_hour": e.get("carbs_per_hour"),
            "tags": e.get("tags"),
            "library_workout_id": e.get("plan_workout_id"),
            "sync_errors": e.get("push_errors"),
        }
    )


def shape_workout(w: dict[str, Any], description_limit: int | None = None) -> dict[str, Any]:
    desc = w.get("description")
    if description_limit:
        desc = _truncate(desc, description_limit)
    return _prune(
        {
            "id": w.get("id"),
            "folder_id": w.get("folder_id"),
            "name": w.get("name"),
            "type": w.get("type"),
            "description": desc,
            "planned_min": _mins(w.get("moving_time")),
            "planned_load": w.get("icu_training_load"),
            "IF_pct": w.get("icu_intensity"),
            "indoor": w.get("indoor"),
            "tags": w.get("tags"),
            "plan_day": w.get("day"),
        }
    )


WELLNESS_FIELDS = {
    "weight": "weight_kg",
    "restingHR": "resting_hr",
    "hrv": "hrv_rmssd",
    "hrvSDNN": "hrv_sdnn",
    "sleepScore": "sleep_score",
    "sleepQuality": "sleep_quality",
    "avgSleepingHR": "sleeping_hr",
    "soreness": "soreness",
    "fatigue": "fatigue",
    "stress": "stress",
    "mood": "mood",
    "motivation": "motivation",
    "injury": "injury",
    "readiness": "readiness",
    "kcalConsumed": "kcal_consumed",
    "bodyFat": "body_fat_pct",
    "vo2max": "vo2max",
    "steps": "steps",
    "spO2": "spo2",
    "respiration": "respiration",
    "comments": "comments",
}


def shape_wellness(w: dict[str, Any]) -> dict[str, Any]:
    ctl, atl = w.get("ctl"), w.get("atl")
    row: dict[str, Any] = {
        "date": w.get("id"),
        "fitness_ctl": ctl,
        "fatigue_atl": atl,
        "form_tsb": (ctl - atl) if isinstance(ctl, (int, float)) and isinstance(atl, (int, float)) else None,
        "ramp_rate": w.get("rampRate"),
        "sleep_h": round(w["sleepSecs"] / 3600, 2) if isinstance(w.get("sleepSecs"), (int, float)) else None,
    }
    for src, dst in WELLNESS_FIELDS.items():
        row[dst] = w.get(src)
    return _prune(row)


def downsample(time: list[Any] | None, series: dict[str, list[Any]], step_s: int, max_points: int) -> dict[str, Any]:
    """Average each stream into buckets of `step_s` seconds (by the time stream if present)."""
    n = max((len(v) for v in series.values()), default=0)
    if n == 0:
        return {"time_s": [], **{k: [] for k in series}}
    t = time if time and len(time) == n else list(range(n))
    duration = (t[-1] or 0) - (t[0] or 0)
    step = max(step_s, 1)
    if duration / step > max_points:
        step = math.ceil(duration / max_points)

    buckets: dict[int, dict[str, list[float]]] = {}
    for idx in range(n):
        b = int(((t[idx] or 0) - (t[0] or 0)) // step)
        slot = buckets.setdefault(b, {k: [] for k in series})
        for k, values in series.items():
            v = values[idx] if idx < len(values) else None
            if isinstance(v, (int, float)):
                slot[k].append(v)

    keys = sorted(buckets)
    out: dict[str, Any] = {"step_s": step, "time_s": [k * step for k in keys]}
    for k in series:
        out[k] = [round(sum(buckets[b][k]) / len(buckets[b][k]), 1) if buckets[b][k] else None for b in keys]
    return out


STANDARD_DURATIONS = [5, 15, 30, 60, 120, 180, 300, 480, 600, 720, 1200, 1800, 2700, 3600, 5400, 7200]


def _label_duration(s: int) -> str:
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m" + (f"{s % 60}s" if s % 60 else "")
    h, m = divmod(s // 60, 60)
    return f"{h}h" + (f"{m}m" if m else "")


def shape_power_curve(curve: dict[str, Any], durations: list[int]) -> dict[str, Any]:
    secs = curve.get("secs") or []
    watts = curve.get("values") or curve.get("watts") or []
    wkg = curve.get("watts_per_kg") or []
    lookup = {s: i for i, s in enumerate(secs)}
    best: dict[str, Any] = {}
    for d in durations:
        i = lookup.get(d)
        if i is None:
            # nearest shorter duration we have data for
            candidates = [s for s in secs if s <= d]
            if not candidates:
                continue
            i = lookup[max(candidates)]
        if i < len(watts) and watts[i]:
            entry: dict[str, Any] = {"w": watts[i]}
            if i < len(wkg) and wkg[i]:
                entry["wkg"] = round(wkg[i], 2)
            best[_label_duration(d)] = entry
    return _prune(
        {
            "curve": curve.get("label") or curve.get("id"),
            "from": (curve.get("start_date_local") or "")[:10] or None,
            "to": (curve.get("end_date_local") or "")[:10] or None,
            "best": best,
        }
    )


# ----------------------------------------------------------------------------- registration


def register_tools(mcp: Any, get_client: Callable[[], IntervalsClient]) -> None:
    """Attach all tools to the MCP server."""

    # ------------------------------------------------------------------ context

    @mcp.tool(annotations=READ)
    async def get_athlete_profile() -> str:
        """Start here. Your Intervals.icu profile: today's date in your timezone, FTP / indoor FTP,
        W', LTHR, max HR, power and HR zones, weight, and which devices sync workouts.
        Use this before interpreting any numbers or writing workouts."""
        c = get_client()
        athlete, sports = await asyncio.gather(
            c.get(c.athlete_path), c.get(f"{c.athlete_path}/sport-settings")
        )
        tz = athlete.get("timezone") or "Europe/London"
        try:
            today = datetime.now(ZoneInfo(tz)).date().isoformat()
        except Exception:  # unknown tz string
            today = date.today().isoformat()
        sport_rows = []
        for s in sports or []:
            sport_rows.append(
                _prune(
                    {
                        "types": s.get("types"),
                        "ftp": s.get("ftp"),
                        "indoor_ftp": s.get("indoor_ftp"),
                        "w_prime": s.get("w_prime"),
                        "p_max": s.get("p_max"),
                        "lthr": s.get("lthr"),
                        "max_hr": s.get("max_hr"),
                        "power_zones_pct_ftp": s.get("power_zones"),
                        "power_zone_names": s.get("power_zone_names"),
                        "hr_zones_bpm": s.get("hr_zones"),
                        "hr_zone_names": s.get("hr_zone_names"),
                        "sweet_spot_pct": [s.get("sweet_spot_min"), s.get("sweet_spot_max")]
                        if s.get("sweet_spot_min")
                        else None,
                    }
                )
            )
        syncs = {
            svc: _prune(
                {
                    "activities_in": athlete.get(f"{key}_sync_activities"),
                    "workouts_out": athlete.get(f"{key}_upload_workouts"),
                }
            )
            for svc, key in [("wahoo", "wahoo"), ("zwift", "zwift"), ("garmin", "icu_garmin"),
                             ("coros", "coros"), ("suunto", "suunto")]
        }
        if athlete.get("icu_garmin_upload_workouts") is not None:
            syncs["garmin"]["workouts_out"] = athlete.get("icu_garmin_upload_workouts")
        return _dump(
            _prune(
                {
                    "today": today,
                    "timezone": tz,
                    "name": athlete.get("name"),
                    "weight_kg": athlete.get("icu_weight") or athlete.get("weight"),
                    "resting_hr": athlete.get("icu_resting_hr"),
                    "sports": sport_rows,
                    "device_sync": {k: v for k, v in syncs.items() if v},
                    "strava_linked": True if athlete.get("strava_id") else None,
                }
            )
        )

    # ------------------------------------------------------------------ activities

    @mcp.tool(annotations=READ)
    async def list_activities(
        oldest: Annotated[str, Field(description="First date to include, YYYY-MM-DD")],
        newest: Annotated[str | None, Field(description="Last date to include, YYYY-MM-DD (default: today)")] = None,
        limit: Annotated[int, Field(ge=1, le=200, description="Max activities to return")] = 60,
    ) -> str:
        """Completed rides and other activities in a date range (newest first): duration, distance,
        load, IF, NP/avg power, HR, cadence, RPE/feel, your notes, decoupling, EF.
        Use get_activity for intervals and zone breakdown of a single ride."""
        c = get_client()
        rows = await c.get(
            f"{c.athlete_path}/activities",
            oldest=_check_date(oldest, "oldest"),
            newest=_check_date(newest, "newest") if newest else None,
            limit=limit,
            fields=",".join(ACTIVITY_LIST_FIELDS),
        )
        acts = [shape_activity_summary(a) for a in rows or []]
        return _dump({"count": len(acts), "activities": acts})

    @mcp.tool(annotations=READ)
    async def search_activities(
        query: Annotated[str, Field(description="Text to match in activity names/tags, e.g. 'Box Hill' or 'race'")],
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> str:
        """Find past activities by name or tag. Returns ids and dates to use with get_activity."""
        c = get_client()
        rows = await c.get(f"{c.athlete_path}/activities/search", q=query, limit=limit)
        return _dump(
            [
                _prune(
                    {
                        "id": r.get("id"),
                        "date": r.get("start_date_local"),
                        "type": r.get("type"),
                        "name": r.get("name"),
                        "moving_min": _mins(r.get("moving_time")),
                        "km": _km(r.get("distance")),
                        "load": r.get("icu_training_load"),
                        "tags": r.get("tags"),
                    }
                )
                for r in rows or []
            ]
        )

    @mcp.tool(annotations=READ)
    async def get_activity(
        activity_id: Annotated[str, Field(description="Activity id, e.g. 'i12345678'")],
        include_comments: Annotated[bool, Field(description="Include the activity's chat/comments")] = True,
    ) -> str:
        """One activity in full: summary, every interval/lap (power, HR, cadence, W'bal, decoupling),
        time in power and HR zones, your notes and comments, and the planned workout it was paired with."""
        c = get_client()
        act = await c.get(f"/activity/{activity_id}", intervals="true")
        out = shape_activity_summary(act, description_limit=4000)
        out["intervals"] = [shape_interval(i) for i in act.get("icu_intervals") or [] if i]
        zt = act.get("icu_zone_times")
        if zt:
            out["time_in_power_zones_min"] = {
                (z.get("id") or f"Z{n + 1}"): _mins(z.get("secs")) or 0 for n, z in enumerate(zt) if isinstance(z, dict)
            }
        hzt = act.get("icu_hr_zone_times")
        if hzt:
            out["time_in_hr_zones_min"] = {f"Z{n + 1}": _mins(s) or 0 for n, s in enumerate(hzt)}
        extras = _prune(
            {
                "joules_above_ftp_kj": round(act["icu_joules_above_ftp"] / 1000, 1)
                if isinstance(act.get("icu_joules_above_ftp"), (int, float))
                else None,
                "max_wbal_depletion_j": act.get("icu_max_wbal_depletion"),
                "polarization_index": act.get("polarization_index"),
                "power_meter": act.get("power_meter") or act.get("device_name"),
                "avg_temp_c": act.get("average_temp") or act.get("average_weather_temp"),
                "ftp_estimate_w": act.get("icu_pm_ftp_watts") or act.get("icu_pm_ftp"),
                "fitness_ctl_after": act.get("icu_ctl"),
                "fatigue_atl_after": act.get("icu_atl"),
                "streams_available": act.get("stream_types"),
            }
        )
        out.update(extras)
        if include_comments:
            try:
                msgs = await c.get(f"/activity/{activity_id}/messages", limit=30)
                out["comments"] = [
                    _prune({"by": m.get("name"), "at": m.get("created"), "text": m.get("content")})
                    for m in msgs or []
                    if m.get("content")
                ]
            except IntervalsError:
                pass
        return _dump(_prune(out))

    @mcp.tool(annotations=READ)
    async def get_activity_streams(
        activity_id: Annotated[str, Field(description="Activity id")],
        streams: Annotated[
            list[str],
            Field(description="Streams to fetch: watts, heartrate, cadence, velocity_smooth, altitude, "
                  "w_bal, temp, grade_smooth, distance"),
        ] = ["watts", "heartrate", "cadence"],
        resolution_s: Annotated[int, Field(ge=1, le=600, description="Average into buckets of this many seconds")] = 10,
        max_points: Annotated[int, Field(ge=50, le=3000)] = 900,
    ) -> str:
        """Second-by-second data for one activity, averaged into buckets so it's ready to chart
        (e.g. power + HR over time, cadence drift, W'bal). Returns aligned arrays sharing time_s."""
        c = get_client()
        wanted = list(dict.fromkeys(["time", *streams]))
        data = await c.get(f"/activity/{activity_id}/streams.json", types=",".join(wanted))
        by_type = {s.get("type"): s.get("data") for s in data or [] if isinstance(s, dict)}
        if "watts" in streams and "watts" not in by_type and "fixed_watts" in by_type:
            by_type["watts"] = by_type["fixed_watts"]
        series = {k: by_type[k] for k in streams if by_type.get(k)}
        missing = [k for k in streams if k not in series]
        out = downsample(by_type.get("time"), series, resolution_s, max_points)
        if missing:
            out["not_recorded"] = missing
        return _dump(out)

    # ------------------------------------------------------------------ fitness & wellness

    @mcp.tool(annotations=READ)
    async def get_fitness_and_wellness(
        oldest: Annotated[str, Field(description="First date, YYYY-MM-DD")],
        newest: Annotated[str | None, Field(description="Last date, YYYY-MM-DD (default: today)")] = None,
    ) -> str:
        """Daily fitness (CTL), fatigue (ATL), form (TSB = CTL - ATL) and ramp rate, plus everything you
        log as wellness: weight, resting HR, HRV, sleep, soreness, fatigue, stress, mood, motivation,
        comments. One row per day - good for trend charts and readiness questions."""
        c = get_client()
        rows = await c.get(
            f"{c.athlete_path}/wellness.json",
            oldest=_check_date(oldest, "oldest"),
            newest=_check_date(newest, "newest") if newest else None,
        )
        days = [shape_wellness(r) for r in rows or []]
        return _dump({"days": days})

    @mcp.tool(annotations=READ)
    async def get_power_curve(
        curves: Annotated[
            list[str],
            Field(description="Periods to compare: '42d', '90d', '1y', 's0' (this season), 's1' (last season), "
                  "'all', or a range 'r.2026-03-01.2026-06-30'"),
        ] = ["42d", "1y"],
        sport: Annotated[str, Field(description="Ride, VirtualRide, Run...")] = "Ride",
        durations_s: Annotated[
            list[int] | None, Field(description="Durations in seconds to report (default: 5s to 2h)")
        ] = None,
    ) -> str:
        """Best power (and W/kg) at key durations for one or more periods - for comparing form across
        periods, spotting strengths, and plotting a power-duration chart."""
        c = get_client()
        data = await c.get(f"{c.athlete_path}/power-curves.json", type=sport, curves=",".join(curves))
        lst = (data or {}).get("list") if isinstance(data, dict) else data
        durs = durations_s or STANDARD_DURATIONS
        return _dump([shape_power_curve(cv, durs) for cv in lst or []])

    # ------------------------------------------------------------------ calendar & library

    @mcp.tool(annotations=READ)
    async def get_calendar(
        oldest: Annotated[str, Field(description="First date, YYYY-MM-DD")],
        newest: Annotated[str | None, Field(description="Last date, YYYY-MM-DD (default: oldest + 6 days)")] = None,
        categories: Annotated[
            list[str] | None,
            Field(description="Filter: WORKOUT, RACE_A, RACE_B, RACE_C, NOTE, HOLIDAY, SICK, INJURED, TARGET"),
        ] = None,
    ) -> str:
        """Planned workouts, races, notes and other calendar entries, including each workout's full
        step text. Event ids from here are what update_planned_workout / delete_calendar_event take."""
        c = get_client()
        rows = await c.get(
            f"{c.athlete_path}/events.json",
            oldest=_check_date(oldest, "oldest"),
            newest=_check_date(newest, "newest") if newest else None,
            category=",".join(categories) if categories else None,
        )
        return _dump([shape_event(e) for e in rows or []])

    @mcp.tool(annotations=READ)
    async def list_workout_library(
        include_descriptions: Annotated[
            bool, Field(description="Include each workout's step text (longer output)")
        ] = False,
    ) -> str:
        """Your saved workout library and training plans: folders and the workouts in them.
        Use get_library_workout for a single workout's full steps."""
        c = get_client()
        folders = await c.get(f"{c.athlete_path}/folders")
        out = []
        for f in folders or []:
            out.append(
                _prune(
                    {
                        "folder_id": f.get("id"),
                        "name": f.get("name"),
                        "kind": f.get("type"),
                        "description": _truncate(f.get("description"), 300),
                        "workouts": [
                            shape_workout(w, None if include_descriptions else 1)
                            if include_descriptions
                            else _prune(
                                {
                                    "id": w.get("id"),
                                    "name": w.get("name"),
                                    "type": w.get("type"),
                                    "planned_min": _mins(w.get("moving_time")),
                                    "planned_load": w.get("icu_training_load"),
                                    "indoor": w.get("indoor"),
                                }
                            )
                            for w in f.get("children") or []
                        ],
                    }
                )
            )
        return _dump(out)

    @mcp.tool(annotations=READ)
    async def get_library_workout(
        workout_id: Annotated[int, Field(description="Workout id from list_workout_library")],
    ) -> str:
        """One saved library workout with its full step text, duration and load."""
        c = get_client()
        w = await c.get(f"{c.athlete_path}/workouts/{workout_id}")
        return _dump(shape_workout(w))

    # ------------------------------------------------------------------ writes: calendar

    @mcp.tool(annotations=WRITE, description="Add a planned workout (or note/race) to the Intervals.icu "
              "calendar. It then syncs to Wahoo/Garmin/Zwift if you've enabled that in Intervals.\n\n" + WORKOUT_SYNTAX)
    async def create_planned_workout(
        workout_date: Annotated[str, Field(description="Date, YYYY-MM-DD")],
        name: Annotated[str, Field(description="Workout name")],
        description: Annotated[
            str, Field(description="Workout steps in Intervals.icu text format, and/or notes. "
                       "Leave empty when using from_library_workout_id.")
        ] = "",
        sport: Annotated[str, Field(description="Ride, VirtualRide, Run, WeightTraining...")] = "Ride",
        category: Annotated[
            Literal["WORKOUT", "NOTE", "RACE_A", "RACE_B", "RACE_C"], Field(description="Calendar entry type")
        ] = "WORKOUT",
        indoor: Annotated[bool | None, Field(description="True for trainer sessions")] = None,
        from_library_workout_id: Annotated[
            int | None, Field(description="Optional: copy steps from this library workout instead of description")
        ] = None,
    ) -> str:
        c = get_client()
        body: dict[str, Any] = {
            "start_date_local": f"{_check_date(workout_date, 'workout_date')}T00:00:00",
            "category": category,
            "type": sport,
            "name": name,
            "description": description,
        }
        if indoor is not None:
            body["indoor"] = indoor
        if from_library_workout_id is not None:
            lib = await c.get(f"{c.athlete_path}/workouts/{from_library_workout_id}")
            if not description.strip():
                body["description"] = lib.get("description") or ""
            if indoor is None and lib.get("indoor") is not None:
                body["indoor"] = lib["indoor"]
        elif not description.strip():
            raise IntervalsError("Pass the workout steps in description, or a from_library_workout_id.")
        created = await c.request("POST", f"{c.athlete_path}/events", json=body)
        return _dump({"created": shape_event(created or {})})

    @mcp.tool(annotations=UPDATE, description="Change a calendar entry: rename, move date, or rewrite the "
              "steps. Only the fields you pass are changed.\n\n" + WORKOUT_SYNTAX)
    async def update_planned_workout(
        event_id: Annotated[int, Field(description="Event id from get_calendar")],
        workout_date: Annotated[str | None, Field(description="New date, YYYY-MM-DD")] = None,
        name: Annotated[str | None, Field(description="New name")] = None,
        description: Annotated[str | None, Field(description="New full step text/notes (replaces the old text)")] = None,
        indoor: Annotated[bool | None, Field(description="True for trainer sessions")] = None,
    ) -> str:
        c = get_client()
        body: dict[str, Any] = {}
        if workout_date:
            body["start_date_local"] = f"{_check_date(workout_date, 'workout_date')}T00:00:00"
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if indoor is not None:
            body["indoor"] = indoor
        if not body:
            raise IntervalsError("Nothing to change - pass at least one of workout_date, name, description, indoor.")
        before = shape_event(await c.get(f"{c.athlete_path}/events/{event_id}"))
        updated = await c.request("PUT", f"{c.athlete_path}/events/{event_id}", json=body)
        return _dump({"before": before, "after": shape_event(updated or {})})

    @mcp.tool(annotations=DELETE)
    async def delete_calendar_event(
        event_id: Annotated[int, Field(description="Event id from get_calendar")],
    ) -> str:
        """Remove one planned workout/note/race from the calendar. Completed activities are not affected."""
        c = get_client()
        before = shape_event(await c.get(f"{c.athlete_path}/events/{event_id}"))
        await c.request("DELETE", f"{c.athlete_path}/events/{event_id}")
        return _dump({"deleted": before})

    # ------------------------------------------------------------------ writes: library

    @mcp.tool(annotations=WRITE, description="Save a new workout into your library (a folder from "
              "list_workout_library), e.g. a refined version of an existing session.\n\n" + WORKOUT_SYNTAX)
    async def create_library_workout(
        folder_id: Annotated[int, Field(description="Folder id from list_workout_library")],
        name: Annotated[str, Field(description="Workout name")],
        description: Annotated[str, Field(description="Workout steps in Intervals.icu text format")],
        sport: Annotated[str, Field(description="Ride, VirtualRide, Run...")] = "Ride",
        indoor: Annotated[bool | None, Field(description="True for trainer sessions")] = None,
    ) -> str:
        c = get_client()
        body: dict[str, Any] = {"folder_id": folder_id, "name": name, "description": description, "type": sport}
        if indoor is not None:
            body["indoor"] = indoor
        created = await c.request("POST", f"{c.athlete_path}/workouts", json=body)
        return _dump({"created": shape_workout(created or {})})

    @mcp.tool(annotations=UPDATE, description="Edit a saved library workout in place - for fine-tuning your "
              "pre-built sessions. Only the fields you pass are changed; the response shows before and after. "
              "To keep the original, use create_library_workout for a new version instead.\n\n" + WORKOUT_SYNTAX)
    async def update_library_workout(
        workout_id: Annotated[int, Field(description="Workout id from list_workout_library")],
        name: Annotated[str | None, Field(description="New name")] = None,
        description: Annotated[str | None, Field(description="New full step text (replaces the old steps)")] = None,
        indoor: Annotated[bool | None, Field(description="True for trainer sessions")] = None,
    ) -> str:
        c = get_client()
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if indoor is not None:
            body["indoor"] = indoor
        if not body:
            raise IntervalsError("Nothing to change - pass at least one of name, description, indoor.")
        before = shape_workout(await c.get(f"{c.athlete_path}/workouts/{workout_id}"))
        updated = await c.request("PUT", f"{c.athlete_path}/workouts/{workout_id}", json=body)
        return _dump({"before": before, "after": shape_workout(updated or {})})

    # ------------------------------------------------------------------ writes: logging

    @mcp.tool(annotations=UPDATE)
    async def update_activity_notes(
        activity_id: Annotated[str, Field(description="Activity id")],
        name: Annotated[str | None, Field(description="New activity name")] = None,
        notes: Annotated[str | None, Field(description="New description/notes (replaces existing)")] = None,
        rpe: Annotated[int | None, Field(ge=1, le=10, description="Perceived exertion 1-10")] = None,
        feel: Annotated[int | None, Field(ge=1, le=5, description="Feel 1 (strong) to 5 (weak), as in Intervals")] = None,
    ) -> str:
        """Update the name, notes, RPE or feel on a completed activity."""
        c = get_client()
        body = {k: v for k, v in {"name": name, "description": notes, "icu_rpe": rpe, "feel": feel}.items() if v is not None}
        if not body:
            raise IntervalsError("Nothing to change - pass name, notes, rpe or feel.")
        updated = await c.request("PUT", f"/activity/{activity_id}", json=body)
        return _dump({"updated": shape_activity_summary(updated or {}, description_limit=4000)})

    @mcp.tool(annotations=UPDATE)
    async def log_wellness(
        day: Annotated[str, Field(description="Date, YYYY-MM-DD")],
        weight_kg: float | None = None,
        resting_hr: int | None = None,
        hrv_rmssd: float | None = None,
        sleep_hours: float | None = None,
        soreness: Annotated[int | None, Field(ge=1, le=4)] = None,
        fatigue: Annotated[int | None, Field(ge=1, le=4)] = None,
        stress: Annotated[int | None, Field(ge=1, le=4)] = None,
        mood: Annotated[int | None, Field(ge=1, le=4)] = None,
        motivation: Annotated[int | None, Field(ge=1, le=4)] = None,
        comments: str | None = None,
    ) -> str:
        """Record wellness for a day (only the fields you pass). Subjective fields use the same
        1-4 scale as the Intervals.icu wellness form - confirm the meaning with the user if unsure."""
        c = get_client()
        d = _check_date(day, "day")
        body = {
            "weight": weight_kg,
            "restingHR": resting_hr,
            "hrv": hrv_rmssd,
            "sleepSecs": int(sleep_hours * 3600) if sleep_hours is not None else None,
            "soreness": soreness,
            "fatigue": fatigue,
            "stress": stress,
            "mood": mood,
            "motivation": motivation,
            "comments": comments,
        }
        body = {k: v for k, v in body.items() if v is not None}
        if not body:
            raise IntervalsError("Nothing to log - pass at least one field.")
        updated = await c.request("PUT", f"{c.athlete_path}/wellness/{d}", json=body)
        return _dump({"saved": shape_wellness(updated or {"id": d, **body})})
