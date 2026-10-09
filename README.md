# intervals-mcp

A personal Claude connector for Intervals.icu. You add it once in Claude, and it then works
on web, desktop and phone, with read and write access to your training.

## What Claude can do with it

| Area | Tools |
|---|---|
| Context | `get_athlete_profile`: today's date, FTP, zones, which devices sync |
| Rides | `list_activities`, `search_activities`, `get_activity` (intervals, zones, notes, comments) |
| Charts | `get_activity_streams` (power/HR/cadence over time), `get_fitness_and_wellness` (CTL/ATL/TSB and wellness), `get_power_curve` |
| Plan | `get_calendar`, `list_workout_library`, `get_library_workout` |
| Write | `create_planned_workout`, `update_planned_workout`, `delete_calendar_event`, `create_library_workout`, `update_library_workout`, `update_activity_notes`, `log_wellness` |

Write tools are flagged as writes, so Claude asks before using them. Planned workouts sync to
Wahoo, Garmin or Zwift through the connections you've already set up in Intervals.

## Deploy (Render, free)

1. On render.com, sign in with GitHub, then choose **New → Blueprint** and pick this repo.
2. Fill in the three values it asks for:
   - `INTERVALS_API_KEY`: in Intervals.icu, go to **Settings → Developer Settings** and generate an API key
   - `INTERVALS_ATHLETE_ID`: the `i123456` part of your Intervals profile URL
   - `CONNECTOR_PASSWORD`: a password you choose, at least 12 characters
   (`TOKEN_SECRET` is generated automatically.)
3. Deploy. Opening `https://<your-service>.onrender.com/` should show `{"ok":true,...}`.

## Connect Claude

In Claude, go to **Customize → Connectors → + → Add custom connector**:
- Name: `intervals` (no dot in the name)
- URL: `https://<your-service>.onrender.com/mcp`
- Leave the OAuth client ID and secret blank.

Click **Connect**, enter your `CONNECTOR_PASSWORD` on the page that opens, and you're done.
The connection then shows up on all your devices.

## Notes

- **Free tier:** the service sleeps after about 15 minutes idle, so the first question after that takes around 30–60 seconds while it wakes.
  Staying signed in survives the sleep, because tokens are signed rather than stored.
- **Revoke everything:** change `TOKEN_SECRET` in Render, then reconnect.
- **Strava:** rides that reach Intervals only via Strava can't be read through the API. Sync your
  head unit directly to Intervals.icu instead.

## Develop

```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```
The tests use a fake Intervals API and run the full Claude sign-in flow, including a restart.
