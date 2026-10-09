"""The connector must only answer to you."""

from conftest import PUBLIC, mcp_call, running_app, sign_in

from intervals_mcp.config import Settings


async def test_mcp_refuses_requests_without_a_token(settings, fake):
    async with running_app(settings, fake) as http:
        r = await mcp_call(http, None, "tools/list")
        assert r.status_code == 401
        assert "resource_metadata" in r.headers.get("www-authenticate", "")


async def test_mcp_refuses_forged_token(settings, fake):
    async with running_app(settings, fake) as http:
        r = await mcp_call(http, "a.eyJjaWQiOiJ4In0.forged", "tools/list")
        assert r.status_code == 401


async def test_full_sign_in_flow_then_tools_work(settings, fake):
    async with running_app(settings, fake) as http:
        tok = await sign_in(http)
        assert tok["access_token"].startswith("a.")
        await mcp_call(http, tok["access_token"], "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        r = await mcp_call(http, tok["access_token"], "tools/list", msg_id=2)
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert {"get_athlete_profile", "list_activities", "create_planned_workout",
                "update_library_workout", "get_power_curve"} <= names


async def test_wrong_password_is_rejected(settings, fake):
    async with running_app(settings, fake) as http:
        result = await sign_in(http, password="nope-nope-nope")
        assert result["login_status"] == 401
        assert "That password" in result["login_page"]


async def test_auth_code_cannot_be_reused(settings, fake):
    async with running_app(settings, fake) as http:
        tok = await sign_in(http)
        c = tok["client"]
        again = await http.post("/token", data={
            "grant_type": "authorization_code", "code": tok["code"],
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "client_id": c["client_id"], "client_secret": c["client_secret"], "code_verifier": tok["verifier"]})
        assert again.status_code == 400


async def test_refresh_token_works(settings, fake):
    async with running_app(settings, fake) as http:
        tok = await sign_in(http)
        c = tok["client"]
        r = await http.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
            "client_id": c["client_id"], "client_secret": c["client_secret"]})
        assert r.status_code == 200, r.text
        assert r.json()["access_token"].startswith("a.")


async def test_session_survives_restart(settings, fake):
    """Render's free tier restarts the app after idling - you must stay signed in."""
    async with running_app(settings, fake) as http:
        tok = await sign_in(http)
    async with running_app(settings, fake) as http:  # brand-new process state
        r = await mcp_call(http, tok["access_token"], "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        assert r.status_code == 200
        c = tok["client"]
        r = await http.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
            "client_id": c["client_id"], "client_secret": c["client_secret"]})
        assert r.status_code == 200, r.text


async def test_changing_secret_revokes_everything(settings, fake):
    async with running_app(settings, fake) as http:
        tok = await sign_in(http)
    rotated = Settings(**{**settings.__dict__, "token_secret": "u" * 48})
    async with running_app(rotated, fake) as http:
        r = await mcp_call(http, tok["access_token"], "tools/list")
        assert r.status_code == 401


async def test_only_claude_redirects_allowed(settings, fake):
    async with running_app(settings, fake) as http:
        r = await http.post("/register", json={
            "client_name": "evil", "redirect_uris": ["https://evil.example/cb"],
            "grant_types": ["authorization_code"], "response_types": ["code"]})
        assert r.status_code == 400


async def test_metadata_points_at_this_server(settings, fake):
    async with running_app(settings, fake) as http:
        r = await http.get("/.well-known/oauth-protected-resource/mcp")
        assert r.json()["resource"] == f"{PUBLIC}/mcp"
        r = await http.get("/.well-known/oauth-authorization-server")
        assert r.json()["authorization_endpoint"] == f"{PUBLIC}/authorize"


async def test_lockout_after_repeated_failures(settings, fake):
    async with running_app(settings, fake) as http:
        for _ in range(10):
            await sign_in(http, password="wrong-wrong-wrong")
        result = await sign_in(http)  # even the right password is refused while locked
        assert result["login_status"] == 429
