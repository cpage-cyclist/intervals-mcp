"""App assembly: MCP server + OAuth + login page + health check."""

from __future__ import annotations

import os

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .auth import SingleUserOAuthProvider, make_login_handler
from .client import IntervalsClient
from .config import Settings
from .tools import register_tools

INSTRUCTIONS = """\
Live access to the user's Intervals.icu account: completed rides, planned workouts, the workout
library, fitness/fatigue/form, wellness logs and power curves. The user is a competitive cyclist.

How to work with it:
- Call get_athlete_profile first in a conversation: it gives today's date, FTP, zones and which
  devices sync. Interpret watts against the FTP in effect at the time (each activity has ftp_at_time).
- Dates are the athlete's local dates, YYYY-MM-DD.
- For charts: get_activity_streams (one ride over time), get_fitness_and_wellness (CTL/ATL/TSB and
  wellness trends), get_power_curve (power-duration). Plot the returned arrays directly.
- Writes change the user's real calendar/library and sync to their head unit. Before calling any
  write tool, show the user what you're about to change (date, name, full steps) and get a yes.
  After a write, check the returned planned_min / planned_load to confirm Intervals parsed the steps.
- When refining a saved session, prefer showing a before/after; offer to save as a new library
  workout if they may want to keep the original.
"""


def build_app(settings: Settings | None = None, client: IntervalsClient | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    intervals = client or IntervalsClient(
        settings.intervals_api_key, settings.athlete_id, settings.intervals_base_url
    )

    provider = SingleUserOAuthProvider(
        secret=settings.token_secret,
        password=settings.connector_password,
        public_url=settings.public_url,
        allowed_redirect_hosts=settings.allowed_redirect_hosts,
        access_ttl=settings.access_token_ttl,
        refresh_ttl=settings.refresh_token_ttl,
    )

    mcp = MCPServer(
        name="intervals",
        title="Intervals.icu",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=f"{settings.public_url}/mcp",
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        ),
    )
    register_tools(mcp, lambda: intervals)

    login = make_login_handler(provider)
    mcp.custom_route("/login", methods=["GET", "POST"], include_in_schema=False)(login)

    @mcp.custom_route("/", methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True, "service": "intervals-mcp"})

    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # Host-header checks are aimed at local servers; this one is public and every
        # MCP request must carry a valid signed token anyway.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
