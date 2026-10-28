from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Literal

from .auth import AuthService, OAuthError, csrf_token
from .i18n import CATALOG, language
from .models import _snowflake


class AccessGrant(BaseModel):
    role: Literal["viewer", "operator"] | None = None


class UserBlock(BaseModel):
    blocked: bool


def auth_router(auth: AuthService, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get("/login")
    async def login_page(request: Request):
        if request.state.identity:
            return RedirectResponse(auth.settings.panel_url("/"), status_code=303)
        error = request.query_params.get("error")
        error_key = {
            "oauth": "Discord sign-in failed. Please try again.",
            "state": "The sign-in link expired or is invalid. Please try again.",
            "blocked": "Your access to the panel has been blocked",
            "denied": "Discord sign-in was cancelled.",
        }.get(error, "")
        return templates.TemplateResponse(request=request, name="login.html", context={
            "translation_catalog": CATALOG, "error_key": error_key,
        })

    @router.get("/auth/discord/login")
    async def begin(request: Request):
        if not auth.enabled:
            raise HTTPException(404, "Discord login is not enabled")
        if request.state.identity:
            return RedirectResponse(auth.settings.panel_url("/"), status_code=303)
        url, binding = auth.begin_login(language(request.query_params.get("lang")))
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(auth.flow_cookie, binding, max_age=600, httponly=True, secure=auth.secure_cookie, samesite="lax", path="/")
        return response

    @router.get("/auth/discord/callback")
    async def callback(request: Request):
        if not auth.enabled:
            raise HTTPException(404, "Discord login is not enabled")
        flow = auth.consume_flow(request.query_params.get("state", ""), request.cookies.get(auth.flow_cookie, ""))
        error = "state"
        if flow:
            if request.query_params.get("error"):
                error = "denied"
            elif (request.query_params.get("iss", "https://discord.com") == "https://discord.com"
                    and request.query_params.get("code") and len(request.query_params["code"]) <= 4096):
                try:
                    user = await auth.oauth.identify(request.query_params["code"], flow["verifier"])
                    token = auth.create_session(user, request.cookies.get(auth.session_cookie, ""))
                except OAuthError:
                    error = "oauth"
                except HTTPException as exc:
                    if exc.status_code != 403:
                        raise
                    error = "blocked"
                else:
                    response = RedirectResponse(auth.settings.panel_url("/"), status_code=303)
                    response.set_cookie(auth.session_cookie, token, max_age=auth.settings.session_hours * 3600,
                                        httponly=True, secure=auth.secure_cookie, samesite="lax", path="/")
                    response.delete_cookie(auth.flow_cookie, path="/", secure=auth.secure_cookie, httponly=True, samesite="lax")
                    return response
            else:
                error = "oauth"
        response = RedirectResponse(auth.settings.panel_url("/login?error=") + error, status_code=303)
        response.delete_cookie(auth.flow_cookie, path="/", secure=auth.secure_cookie, httponly=True, samesite="lax")
        return response

    @router.get("/api/auth/me")
    async def me(request: Request):
        identity = request.state.identity
        return {
            "mode": auth.settings.auth_mode, "authenticated": identity is not None,
            "owner": bool(identity and identity.owner),
            "user": {"id": identity.id, "display_name": identity.display_name} if identity else None,
            "csrf_token": csrf_token(request.cookies[auth.session_cookie]) if auth.enabled and identity else None,
        }

    @router.post("/api/auth/logout")
    async def logout(request: Request):
        auth.logout(request.cookies.get(auth.session_cookie, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.session_cookie, path="/", secure=auth.secure_cookie, httponly=True, samesite="lax")
        return response

    @router.get("/api/admin/users", dependencies=[Depends(auth.owner)])
    async def users():
        return auth.users()

    @router.put("/api/admin/users/{user_id}/bots/{bot_id}", dependencies=[Depends(auth.owner)])
    async def grant(user_id: str, bot_id: str, data: AccessGrant):
        try:
            user_id = _snowflake(user_id)
        except ValueError:
            raise HTTPException(422, "Discord ID must contain only digits") from None
        auth.set_grant(user_id, bot_id, data.role)
        auth.database.scoped(bot_id).audit("access_changed", {"user_id": user_id, "role": data.role or "none"})
        return {"ok": True}

    @router.put("/api/admin/users/{user_id}", dependencies=[Depends(auth.owner)])
    async def block(user_id: str, data: UserBlock):
        try:
            user_id = _snowflake(user_id)
        except ValueError:
            raise HTTPException(422, "Discord ID must contain only digits") from None
        auth.set_blocked(user_id, data.blocked)
        # Copy the user-level change to each existing profile's audit history.
        for profile in auth.database.profiles():
            auth.database.scoped(profile["id"]).audit("user_blocked" if data.blocked else "user_unblocked", {"user_id": user_id})
        return {"ok": True}

    return router
