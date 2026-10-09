"""Single-user OAuth for the Claude connector.

Claude's custom connectors sign in with OAuth. This is a minimal authorization
server with exactly one user (you), who proves who they are with
CONNECTOR_PASSWORD on a small login page.

Everything is *stateless*: client registrations, auth codes and tokens are
HMAC-signed with TOKEN_SECRET instead of being stored. Render's free tier
restarts the app after it idles, and an in-memory store would log you out
every time. To revoke every session at once, change TOKEN_SECRET.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from collections import deque
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

# ----------------------------------------------------------------------------- signing


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    """Compact signed JSON blobs: <payload>.<mac>, bound to a purpose string."""

    def __init__(self, secret: str) -> None:
        self._key = hashlib.sha256(secret.encode()).digest()

    def _mac(self, purpose: str, body: str) -> str:
        return _b64e(hmac.new(self._key, f"{purpose}|{body}".encode(), hashlib.sha256).digest()[:24])

    def sign(self, payload: dict[str, Any], purpose: str) -> str:
        body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
        return f"{body}.{self._mac(purpose, body)}"

    def verify(self, token: str, purpose: str) -> dict[str, Any] | None:
        try:
            body, mac = token.rsplit(".", 1)
        except ValueError:
            return None
        if not hmac.compare_digest(mac, self._mac(purpose, body)):
            return None
        try:
            payload = json.loads(_b64d(body))
        except (ValueError, json.JSONDecodeError):
            return None
        exp = payload.get("exp")
        if exp is not None and exp < time.time():
            return None
        return payload

    def derive(self, label: str) -> str:
        return _b64e(hmac.new(self._key, label.encode(), hashlib.sha256).digest())


# ----------------------------------------------------------------------------- provider


class SingleUserOAuthProvider:
    """Implements mcp's OAuthAuthorizationServerProvider protocol."""

    CLIENT_PREFIX = "c."
    ACCESS_PREFIX = "a."
    REFRESH_PREFIX = "r."
    CODE_PREFIX = "k."

    def __init__(
        self,
        *,
        secret: str,
        password: str,
        public_url: str,
        allowed_redirect_hosts: tuple[str, ...],
        access_ttl: int,
        refresh_ttl: int,
    ) -> None:
        self.signer = Signer(secret)
        self._password = password
        self.public_url = public_url.rstrip("/")
        self.allowed_redirect_hosts = allowed_redirect_hosts
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl
        self._used_codes: dict[str, float] = {}
        self._failures: deque[float] = deque()

    # --------------------------------------------------------------- clients

    def _redirect_allowed(self, uri: str) -> bool:
        host = (urlparse(uri).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.allowed_redirect_hosts)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or not all(self._redirect_allowed(u) for u in uris):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="This connector only accepts sign-ins from Claude.",
            )
        meta = {
            "ru": uris,
            "am": client_info.token_endpoint_auth_method or "client_secret_post",
            "gt": list(client_info.grant_types or ["authorization_code", "refresh_token"]),
            "sc": client_info.scope,
            "n": (client_info.client_name or "")[:60],
            "iat": client_info.client_id_issued_at or int(time.time()),
        }
        # The registration handler returns this same object, so rewriting the id and
        # secret here hands Claude a self-describing, signed client id.
        client_id = self.CLIENT_PREFIX + self.signer.sign(meta, "client")
        client_info.client_id = client_id
        if meta["am"] != "none":
            client_info.client_secret = self.signer.derive("client-secret:" + client_id)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not client_id.startswith(self.CLIENT_PREFIX):
            return None
        meta = self.signer.verify(client_id[len(self.CLIENT_PREFIX):], "client")
        if not meta:
            return None
        auth_method = meta.get("am", "client_secret_post")
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=None if auth_method == "none" else self.signer.derive("client-secret:" + client_id),
            client_id_issued_at=meta.get("iat"),
            client_secret_expires_at=None if auth_method == "none" else 0,
            redirect_uris=meta["ru"],
            token_endpoint_auth_method=auth_method,
            grant_types=meta.get("gt") or ["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=meta.get("sc"),
            client_name=meta.get("n") or None,
        )

    # --------------------------------------------------------------- authorize -> login page

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        pending = {
            "cid": client.client_id,
            "ru": str(params.redirect_uri),
            "rux": params.redirect_uri_provided_explicitly,
            "cc": params.code_challenge,
            "st": params.state,
            "sc": params.scopes or [],
            "res": params.resource,
            "exp": int(time.time()) + 900,
        }
        return f"{self.public_url}/login?req={self.signer.sign(pending, 'pending')}"

    def check_password(self, attempt: str) -> bool:
        now = time.time()
        while self._failures and self._failures[0] < now - 900:
            self._failures.popleft()
        if len(self._failures) >= 10:
            return False
        ok = hmac.compare_digest(attempt.encode(), self._password.encode())
        if not ok:
            self._failures.append(now)
        return ok

    @property
    def locked_out(self) -> bool:
        now = time.time()
        return sum(1 for t in self._failures if t >= now - 900) >= 10

    def issue_code(self, pending: dict[str, Any]) -> str:
        payload = {k: pending[k] for k in ("cid", "ru", "rux", "cc", "sc", "res")}
        payload["exp"] = int(time.time()) + 300
        payload["n"] = secrets.token_hex(8)
        return self.CODE_PREFIX + self.signer.sign(payload, "code")

    # --------------------------------------------------------------- codes

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        if not authorization_code.startswith(self.CODE_PREFIX):
            return None
        data = self.signer.verify(authorization_code[len(self.CODE_PREFIX):], "code")
        if not data or data.get("cid") != client.client_id or authorization_code in self._used_codes:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=data.get("sc") or [],
            expires_at=float(data["exp"]),
            client_id=data["cid"],
            code_challenge=data["cc"],
            redirect_uri=data["ru"],
            redirect_uri_provided_explicitly=bool(data.get("rux")),
            resource=data.get("res"),
            subject="owner",
        )

    def _issue_tokens(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        now = int(time.time())
        base = {"cid": client_id, "sc": scopes, "res": resource, "sub": "owner"}
        access = self.ACCESS_PREFIX + self.signer.sign({**base, "exp": now + self.access_ttl}, "access")
        refresh = self.REFRESH_PREFIX + self.signer.sign(
            {**base, "exp": now + self.refresh_ttl, "n": secrets.token_hex(6)}, "refresh"
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes) if scopes else None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        now = time.time()
        for code, exp in list(self._used_codes.items()):
            if exp < now:
                del self._used_codes[code]
        if authorization_code.code in self._used_codes:
            raise TokenError(error="invalid_grant", error_description="Authorization code already used")
        self._used_codes[authorization_code.code] = authorization_code.expires_at
        return self._issue_tokens(client.client_id, authorization_code.scopes, authorization_code.resource)

    # --------------------------------------------------------------- refresh / access

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        if not refresh_token.startswith(self.REFRESH_PREFIX):
            return None
        data = self.signer.verify(refresh_token[len(self.REFRESH_PREFIX):], "refresh")
        if not data or data.get("cid") != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=data["cid"],
            scopes=data.get("sc") or [],
            expires_at=data.get("exp"),
            resource=data.get("res"),
            subject="owner",
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        return self._issue_tokens(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if not token.startswith(self.ACCESS_PREFIX):
            return None
        data = self.signer.verify(token[len(self.ACCESS_PREFIX):], "access")
        if not data:
            return None
        # The client id is itself signed; if TOKEN_SECRET changed, it no longer verifies.
        if not await self.get_client(data["cid"]):
            return None
        return AccessToken(
            token=token,
            client_id=data["cid"],
            scopes=data.get("sc") or [],
            expires_at=data.get("exp"),
            resource=data.get("res"),
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Stateless tokens can't be revoked one by one; rotate TOKEN_SECRET to revoke all.
        return None


# ----------------------------------------------------------------------------- login page

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Intervals connector sign-in</title>
<style>
:root{{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--muted:#6b6a66;--accent:#d4572a;--err:#b42318}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--card:#1f1f1d;--ink:#eeede9;--muted:#a3a29d;--accent:#f07c50;--err:#ff8a7a}}}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);
color:var(--ink);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;padding:16px}}
main{{background:var(--card);border-radius:14px;padding:28px;width:100%;max-width:380px;box-shadow:0 1px 3px #0002}}
h1{{font-size:20px;margin:0 0 6px}}p{{color:var(--muted);margin:0 0 20px;font-size:14px}}
input{{width:100%;padding:12px;border-radius:8px;border:1px solid #8884;background:transparent;color:inherit;font-size:16px}}
button{{margin-top:14px;width:100%;padding:12px;border:0;border-radius:8px;background:var(--accent);color:#fff;
font-weight:600;font-size:16px;cursor:pointer}}.err{{color:var(--err);font-size:14px;margin:0 0 12px}}
</style></head><body><main>
<h1>Connect Claude to Intervals.icu</h1>
<p>{client} wants access to your training data. Enter your connector password to allow it.</p>
{error}
<form method="post" action="/login">
<input type="hidden" name="req" value="{req}">
<input type="password" name="password" placeholder="Connector password" autocomplete="current-password" autofocus required>
<button type="submit">Allow access</button>
</form></main></body></html>"""


def _page(req: str, client_name: str, error: str = "", status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        _PAGE.format(
            req=html.escape(req, quote=True),
            client=html.escape(client_name or "Claude"),
            error=f'<p class="err">{html.escape(error)}</p>' if error else "",
        ),
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"},
    )


def make_login_handler(provider: SingleUserOAuthProvider):
    async def login(request: Request) -> Response:
        if request.method == "GET":
            req = request.query_params.get("req", "")
        else:
            form = await request.form()
            req = str(form.get("req", ""))
        pending = provider.signer.verify(req, "pending")
        if not pending:
            return HTMLResponse("This sign-in link has expired. Start the connection again from Claude.", status_code=400)
        client = await provider.get_client(pending["cid"])
        if client is None:
            return HTMLResponse("Unknown client. Start the connection again from Claude.", status_code=400)
        name = client.client_name or "Claude"

        if request.method == "GET":
            return _page(req, name)

        if provider.locked_out:
            return _page(req, name, "Too many wrong attempts. Try again in 15 minutes.", status=429)
        attempt = str(form.get("password", ""))
        if not provider.check_password(attempt):
            await asyncio.sleep(1.0)
            return _page(req, name, "That password isn't right.", status=401)

        code = provider.issue_code(pending)
        target = construct_redirect_uri(pending["ru"], code=code, state=pending.get("st"))
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    return login
