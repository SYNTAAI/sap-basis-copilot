"""
SyntaAI MCP Server - OAuth 2.0 Authorization Server Provider

Implements OAuthAuthorizationServerProvider from the MCP Python SDK (1.26+).
Acts as both auth server and resource server (standalone).

Supports:
- Dynamic Client Registration (RFC 7591)
- Authorization Code + PKCE (RFC 7636)
- Token refresh & revocation
- Discovery endpoints (RFC 8414, RFC 9728) - handled by SDK

The SDK's authorize() must return a URL string (not a Response).
We redirect to our own /syntaai-login page, which handles the login form
and POST, then redirects back to the client's redirect_uri with an auth code.

Storage: In-memory with JSON file persistence for restarts.
"""

import json
import os
import secrets
import time
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("syntaai.oauth")

# --- Persistence helpers ---
DATA_DIR = Path(os.getenv("SYNTAAI_DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CLIENTS_FILE = DATA_DIR / "oauth_clients.json"
TOKENS_FILE = DATA_DIR / "oauth_tokens.json"


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def _save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2, default=str))


# --- Login accounts ---
# Real accounts live in a file-backed store ($SYNTAAI_DATA_DIR/users.json),
# managed with manage_users.py. No hard-coded/demo credentials.
from user_store import authenticate as _authenticate_user, user_exists as _user_exists

ISSUER_URL = os.getenv("SYNTAAI_ISSUER_URL", "https://mcp2.syntaai.com")


class SyntaAIOAuthProvider(OAuthAuthorizationServerProvider):
    """
    In-memory OAuth 2.0 Authorization Server for SyntaAI MCP.
    """

    def __init__(self):
        self.clients: dict[str, dict] = {}
        self.auth_codes: dict[str, dict] = {}
        self.access_tokens: dict[str, dict] = {}
        self.refresh_tokens: dict[str, dict] = {}
        # Pending authorization sessions: session_id -> {client, params}
        self.pending_auth: dict[str, dict] = {}
        self._load_persisted_data()
        logger.info("OAuth provider initialized (%d clients)", len(self.clients))

    def _load_persisted_data(self):
        for cid, cdata in _load_json(CLIENTS_FILE).items():
            self.clients[cid] = cdata
        for tk, td in _load_json(TOKENS_FILE).items():
            if td.get("type") == "access":
                self.access_tokens[tk] = td
            elif td.get("type") == "refresh":
                self.refresh_tokens[tk] = td

    def _persist_clients(self):
        try:
            _save_json(CLIENTS_FILE, self.clients)
        except Exception as e:
            logger.warning("Failed to persist clients: %s", e)

    def _persist_tokens(self):
        try:
            all_tokens = {}
            for k, v in self.access_tokens.items():
                all_tokens[k] = {**v, "type": "access"}
            for k, v in self.refresh_tokens.items():
                all_tokens[k] = {**v, "type": "refresh"}
            _save_json(TOKENS_FILE, all_tokens)
        except Exception as e:
            logger.warning("Failed to persist tokens: %s", e)

    # --- Client Registration (RFC 7591) ---
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        logger.info("get_client called with client_id=%s", client_id)
        client_data = self.clients.get(client_id)
        if client_data is None:
            logger.warning("Client %s NOT FOUND (known: %s)", client_id, list(self.clients.keys()))
            return None
        try:
            return OAuthClientInformationFull(**client_data)
        except Exception as e:
            logger.error("Failed to deserialize client %s: %s", client_id, e)
            return None

    async def register_client(
        self, client_info: OAuthClientInformationFull
    ) -> OAuthClientInformationFull:
        now = int(time.time())
        client_data = client_info.model_dump()

        # Use client-provided client_id if present (MCP SDK pre-generates UUIDs),
        # otherwise generate our own
        client_id = client_data.get("client_id") or f"syntaai_{secrets.token_hex(16)}"

        logger.info("register_client — incoming client_id=%s, using client_id=%s",
                     client_data.get("client_id"), client_id)

        if not client_data.get("grant_types"):
            client_data["grant_types"] = ["authorization_code", "refresh_token"]
        if not client_data.get("response_types"):
            client_data["response_types"] = ["code"]
        # Respect client's declared auth method; default to "none" (public client
        # with PKCE) which works with Azure/Copilot Studio and other connectors
        # that don't send client_secret in token requests.
        if not client_data.get("token_endpoint_auth_method"):
            client_data["token_endpoint_auth_method"] = "none"

        # Only generate a client_secret for confidential clients.
        # Public clients (token_endpoint_auth_method="none") must NOT have a
        # client_secret, because the MCP SDK enforces secret validation whenever
        # client.client_secret is set — even if the auth method is "none".
        auth_method = client_data.get("token_endpoint_auth_method", "none")
        if auth_method == "none":
            client_secret = None
            client_secret_expires = 0
        else:
            client_secret = client_data.get("client_secret") or secrets.token_hex(32)
            client_secret_expires = int(time.time()) + 86400 * 365 * 10

        client_data.update({
            "client_id": client_id,
            "client_secret": client_secret,
            "client_id_issued_at": now,
            "client_secret_expires_at": client_secret_expires,
        })

        # Always allow Claude's callback URLs
        claude_callbacks = [
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
            "http://localhost:6274/oauth/callback",
            "http://localhost:6274/oauth/callback/debug",
        ]
        existing_uris = client_data.get("redirect_uris", [])
        for cb in claude_callbacks:
            if cb not in existing_uris:
                existing_uris.append(cb)
        client_data["redirect_uris"] = existing_uris

        self.clients[client_id] = client_data
        self._persist_clients()
        logger.info("Registered client: %s (%s)", client_id, client_data.get("client_name", "?"))
        return OAuthClientInformationFull(**client_data)

    # --- Authorization Endpoint ---
    # SDK calls authorize(client, params) -> str (a redirect URL)
    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        # Store the pending authorization, then return a URL to our login page
        session_id = secrets.token_hex(32)
        self.pending_auth[session_id] = {
            "client_id": client.client_id,
            "client_name": client.client_name or "MCP Client",
            "redirect_uri": str(params.redirect_uri) if params.redirect_uri else None,
            "code_challenge": params.code_challenge,
            "scopes": params.scopes or [],
            "state": params.state,
            "created_at": time.time(),
            "expires_at": time.time() + 600,
        }
        logger.info("Authorization session %s created for client %s", session_id, client.client_id)

        login_url = f"{ISSUER_URL}/syntaai-login?session={session_id}"
        return login_url

    def _issue_auth_code(self, session_id: str, user_email: str, user_name: str, user_role: str) -> str | None:
        """Issue an auth code for a completed login. Returns redirect URL or None."""
        session = self.pending_auth.pop(session_id, None)
        if not session:
            return None
        if time.time() > session.get("expires_at", 0):
            return None

        code = secrets.token_hex(32)
        self.auth_codes[code] = {
            "client_id": session["client_id"],
            "redirect_uri": session["redirect_uri"],
            "code_challenge": session["code_challenge"],
            "scopes": session["scopes"],
            "state": session["state"],
            "user_email": user_email,
            "user_name": user_name,
            "user_role": user_role,
            "created_at": time.time(),
            "expires_at": time.time() + 600,
            "redirect_uri_provided_explicitly": session["redirect_uri"],
        }
        logger.info("Auth code issued for %s -> client %s", user_email, session["client_id"])

        redirect_url = session["redirect_uri"]
        sep = "&" if "?" in redirect_url else "?"
        redirect_url += f"{sep}code={code}"
        if session["state"]:
            redirect_url += f"&state={session['state']}"
        return redirect_url

    # --- Token Exchange ---
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code_data = self.auth_codes.get(authorization_code)
        if not code_data:
            return None
        if time.time() > code_data.get("expires_at", 0):
            del self.auth_codes[authorization_code]
            return None
        if code_data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            client_id=client.client_id,
            redirect_uri=code_data.get("redirect_uri_provided_explicitly"),
            redirect_uri_provided_explicitly=code_data.get("redirect_uri_provided_explicitly") is not None,
            code_challenge=code_data.get("code_challenge", ""),
            scopes=code_data.get("scopes", []),
            expires_at=code_data.get("expires_at", time.time() + 600),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        code_data = self.auth_codes.pop(authorization_code.code, {})
        access_token = f"syntaai_at_{secrets.token_hex(32)}"
        refresh_token = f"syntaai_rt_{secrets.token_hex(32)}"
        expires_in = 3600
        now = time.time()

        self.access_tokens[access_token] = {
            "client_id": client.client_id,
            "user_email": code_data.get("user_email", "unknown"),
            "user_name": code_data.get("user_name", "Unknown"),
            "user_role": code_data.get("user_role", "viewer"),
            "scopes": authorization_code.scopes,
            "created_at": now,
            "expires_at": now + expires_in,
        }
        self.refresh_tokens[refresh_token] = {
            "client_id": client.client_id,
            "user_email": code_data.get("user_email", "unknown"),
            "user_name": code_data.get("user_name", "Unknown"),
            "user_role": code_data.get("user_role", "viewer"),
            "scopes": authorization_code.scopes,
            "created_at": now,
            "expires_at": now + 86400 * 30,
        }
        self._persist_tokens()
        logger.info("Tokens issued for %s", code_data.get("user_email"))
        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=expires_in,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    # --- Refresh Token ---
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        rt = self.refresh_tokens.get(refresh_token)
        if not rt:
            return None
        if time.time() > rt.get("expires_at", 0):
            del self.refresh_tokens[refresh_token]
            self._persist_tokens()
            return None
        if not _user_exists(rt.get("user_email", "")):
            logger.info("load_refresh_token: user %s no longer registered — rejecting", rt.get("user_email"))
            del self.refresh_tokens[refresh_token]
            self._persist_tokens()
            return None
        if rt["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=rt.get("scopes", []),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        rt_data = self.refresh_tokens.pop(refresh_token.token, {})
        new_at = f"syntaai_at_{secrets.token_hex(32)}"
        new_rt = f"syntaai_rt_{secrets.token_hex(32)}"
        expires_in = 3600
        now = time.time()
        eff_scopes = scopes if scopes else rt_data.get("scopes", [])

        self.access_tokens[new_at] = {
            "client_id": client.client_id,
            "user_email": rt_data.get("user_email", "unknown"),
            "user_name": rt_data.get("user_name", "Unknown"),
            "user_role": rt_data.get("user_role", "viewer"),
            "scopes": eff_scopes,
            "created_at": now,
            "expires_at": now + expires_in,
        }
        self.refresh_tokens[new_rt] = {
            "client_id": client.client_id,
            "user_email": rt_data.get("user_email", "unknown"),
            "user_name": rt_data.get("user_name", "Unknown"),
            "user_role": rt_data.get("user_role", "viewer"),
            "scopes": eff_scopes,
            "created_at": now,
            "expires_at": now + 86400 * 30,
        }
        self._persist_tokens()
        logger.info("Tokens refreshed for %s", rt_data.get("user_email"))
        return OAuthToken(
            access_token=new_at,
            token_type="bearer",
            expires_in=expires_in,
            refresh_token=new_rt,
            scope=" ".join(eff_scopes) if eff_scopes else None,
        )

    # --- Token Verification ---
    async def load_access_token(self, token: str) -> AccessToken | None:
        td = self.access_tokens.get(token)
        if not td:
            logger.info("load_access_token: token not found (prefix=%s...)", token[:20])
            return None
        if time.time() > td.get("expires_at", 0):
            logger.info("load_access_token: token expired for %s", td.get("user_email"))
            del self.access_tokens[token]
            self._persist_tokens()
            return None
        if not _user_exists(td.get("user_email", "")):
            logger.info("load_access_token: user %s no longer registered — rejecting", td.get("user_email"))
            del self.access_tokens[token]
            self._persist_tokens()
            return None
        logger.info("load_access_token: valid token for %s (client=%s)", td.get("user_email"), td.get("client_id"))
        return AccessToken(
            token=token,
            client_id=td["client_id"],
            scopes=td.get("scopes", []),
            expires_at=int(td.get("expires_at", 0)),
        )

    # --- Token Revocation (RFC 7009) ---
    async def revoke_token(
        self,
        token: AccessToken | RefreshToken,
    ) -> None:
        revoked = False
        # Determine the token string based on type
        if isinstance(token, AccessToken):
            token_str = token.token
            if token_str in self.access_tokens:
                del self.access_tokens[token_str]
                revoked = True
        elif isinstance(token, RefreshToken):
            token_str = token.token
            if token_str in self.refresh_tokens:
                del self.refresh_tokens[token_str]
                revoked = True
        if revoked:
            self._persist_tokens()
            logger.info("Token revoked: %s for client %s", type(token).__name__, token.client_id)


# ============================================================
# Login page HTML + Starlette route handler
# ============================================================

def _login_page_html(client_name: str, scopes: list[str], error: str = "") -> str:
    scope_html = ""
    if scopes:
        items = "".join(
            f'<li style="margin:4px 0;padding:4px 8px;background:#f0f4ff;border-radius:4px;font-size:14px;">{s}</li>'
            for s in scopes
        )
        scope_html = f'''
        <div style="margin:16px 0;">
            <p style="font-size:14px;color:#555;margin-bottom:8px;">
                <strong>{client_name}</strong> requests access to:
            </p>
            <ul style="list-style:none;padding:0;">{items}</ul>
        </div>'''

    error_html = ""
    if error:
        error_html = f'''
        <div style="background:#fee;border:1px solid #fcc;border-radius:8px;
                    padding:12px;margin-bottom:16px;color:#c33;font-size:14px;">
            {error}
        </div>'''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SyntaAI - Sign In</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:linear-gradient(135deg,#0a1628,#1a365d,#2d4a7a);min-height:100vh;
display:flex;align-items:center;justify-content:center}}
.card{{background:#fff;border-radius:16px;padding:40px;width:100%;max-width:420px;
box-shadow:0 20px 60px rgba(0,0,0,.3)}}
.logo{{text-align:center;margin-bottom:24px}}
.logo h1{{font-size:28px;color:#1a365d;margin-bottom:4px}}
.logo p{{font-size:14px;color:#718096}}
.fg{{margin-bottom:16px}}
label{{display:block;font-size:14px;font-weight:600;color:#374151;margin-bottom:6px}}
input{{width:100%;padding:12px 16px;border:2px solid #e2e8f0;border-radius:8px;font-size:16px}}
input:focus{{outline:none;border-color:#3b82f6}}
button{{width:100%;padding:14px;background:linear-gradient(135deg,#1a365d,#2563eb);
color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer}}
button:hover{{opacity:.9}}
.hint{{margin-top:16px;padding:12px;background:#f0fdf4;border:1px solid #bbf7d0;
border-radius:8px;font-size:12px;color:#166534}}
.foot{{text-align:center;margin-top:20px;font-size:12px;color:#9ca3af}}
</style>
</head>
<body>
<div class="card">
  <div class="logo"><h1>SyntaAI</h1><p>ERP Security Platform</p></div>
  {error_html}
  <p style="font-size:14px;color:#555;margin-bottom:20px;text-align:center">
    Sign in to authorize <strong>{client_name}</strong>
  </p>
  {scope_html}
  <form method="POST">
    <div class="fg"><label for="email">Email</label>
      <input type="email" id="email" name="email" placeholder="you@company.com" required autofocus></div>
    <div class="fg"><label for="password">Password</label>
      <input type="password" id="password" name="password" required></div>
    <button type="submit">Authorize &amp; Continue</button>
  </form>
  <div class="hint">
    Sign in with your SyntaAI account. Don't have one? Ask your administrator
    to register your email.
  </div>
  <div class="foot">
    By signing in you agree to SyntaAI's
    <a href="https://mcp2.syntaai.com/terms" style="color:#3b82f6">Terms</a> &amp;
    <a href="https://mcp2.syntaai.com/privacy" style="color:#3b82f6">Privacy Policy</a>.
  </div>
</div>
</body></html>'''


async def login_page_handler(request: Request) -> HTMLResponse | RedirectResponse:
    """
    Handles GET /syntaai-login?session=xxx  (show login form)
    and POST /syntaai-login?session=xxx    (process login, issue auth code)
    """
    session_id = request.query_params.get("session", "")

    # Get the provider instance from app state
    provider: SyntaAIOAuthProvider = request.app.state.oauth_provider

    session = provider.pending_auth.get(session_id)
    if not session:
        return HTMLResponse("<h1>Invalid or expired session</h1>", status_code=400)
    if time.time() > session.get("expires_at", 0):
        provider.pending_auth.pop(session_id, None)
        return HTMLResponse("<h1>Session expired</h1>", status_code=400)

    client_name = session.get("client_name", "MCP Client")
    scopes = session.get("scopes", [])

    if request.method == "POST":
        form = await request.form()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", "")).strip()

        user = _authenticate_user(email, password)
        if user:
            redirect_url = provider._issue_auth_code(
                session_id, email, user["name"], user["role"]
            )
            if redirect_url:
                return RedirectResponse(url=redirect_url, status_code=302)
            return HTMLResponse("<h1>Failed to issue authorization code</h1>", status_code=500)
        else:
            return HTMLResponse(
                _login_page_html(client_name, scopes, error="Invalid email or password."),
                status_code=200,
            )

    return HTMLResponse(
        _login_page_html(client_name, scopes),
        status_code=200,
    )
