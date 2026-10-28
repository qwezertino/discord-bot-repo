from __future__ import annotations

import binascii
import hmac
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from .config import BASE_DIR, Settings
from .database import Database
from .discord_service import DiscordServiceError
from .bot_manager import BotManager
from .auth import AuthService, csrf_token
from .auth_routes import auth_router
from .audit_context import audit_actor
from .emoji_catalog import unicode_emoji_catalog
from .i18n import CATALOG, language, localized_status, translate, validation_detail
from .logging_filters import install_oauth_log_filter
from .models import BotProfile, MessageImportRequest, MessagePayload, ReactionRoleCreate


class PanelSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, settings: Settings, auth: AuthService) -> None:
        super().__init__(app)
        self.settings = settings
        self.auth = auth

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        locale = language(request.headers.get("accept-language"))
        def reject(status, message):
            return JSONResponse({"detail": translate(message, locale)}, status_code=status)

        if self.auth.enabled:
            canonical = urlsplit(self.settings.oauth_redirect_uri)
            if request.headers.get("host", "").lower() != canonical.netloc.lower() or request.url.scheme != canonical.scheme:
                return reject(400, "Open the panel using its configured address")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            host = request.headers.get("host")
            if origin and (urlsplit(origin).netloc != host or urlsplit(origin).scheme != request.url.scheme):
                return JSONResponse({"detail": translate("Invalid request origin", language(request.headers.get("accept-language")))}, status_code=403)

        if not self.auth.enabled and self.settings.panel_password:
            credentials = request.headers.get("authorization", "")
            expected_username = self.settings.panel_username.encode()
            expected_password = self.settings.panel_password.encode()
            try:
                import base64

                scheme, encoded = credentials.split(" ", 1)
                decoded = base64.b64decode(encoded).decode("utf-8")
                username, password = decoded.split(":", 1)
                valid = scheme.lower() == "basic"
                valid &= hmac.compare_digest(username.encode(), expected_username)
                valid &= hmac.compare_digest(password.encode(), expected_password)
            except (binascii.Error, ValueError, UnicodeError):
                valid = False
            if not valid:
                return Response(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Discord Bot Panel"'},
                )
        identity = self.auth.resolve(request)
        request.state.identity = identity
        path = request.url.path
        public = request.method in {"GET", "HEAD"} and (
            path in {"/login", "/auth/discord/login", "/auth/discord/callback", "/api/auth/me"}
            or path.startswith("/static/")
        )
        if self.auth.enabled and not identity and not public:
            if path.startswith("/api/"):
                return reject(401, "Sign in to continue")
            return RedirectResponse("/login", status_code=303)
        if self.auth.enabled and request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("x-csrf-token", "")
            expected = csrf_token(request.cookies.get(self.auth.session_cookie, ""))
            if not identity or not supplied.isascii() or not hmac.compare_digest(supplied, expected):
                return reject(403, "Invalid security token. Refresh the page and try again.")
        if identity and path.startswith("/api/"):
            try:
                parts = path.split("/")
                if path.startswith("/api/admin/") or (path == "/api/bots" and request.method == "POST"):
                    await self.auth.owner(request)
                elif path.startswith("/api/bots/") and len(parts) > 3:
                    self.auth.bot_access(request, parts[3])
                    if request.method not in {"GET", "HEAD", "OPTIONS"} and (len(parts) == 4 or parts[4] == "reconnect"):
                        await self.auth.owner(request)
            except HTTPException as exc:
                return reject(exc.status_code, exc.detail)
        actor = audit_actor.set(identity.id if identity else None)
        try:
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store" if not path.startswith("/static/") else "no-cache"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        finally:
            audit_actor.reset(actor)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    settings.validate_security()
    install_oauth_log_filter()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    database = Database(settings.data_dir / "discord_bot.sqlite3")
    database.initialize(settings.allow_privileged_roles)
    manager = BotManager(settings, database)
    manager.bootstrap_profiles()
    auth = AuthService(settings, database)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            await manager.close()

    app = FastAPI(title="Discord Bot Panel", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.bots = manager
    app.state.auth = auth
    app.add_middleware(PanelSecurityMiddleware, settings=settings, auth=auth)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
    app.include_router(auth_router(auth, templates))

    @app.exception_handler(DiscordServiceError)
    async def discord_error(request: Request, exc: DiscordServiceError) -> JSONResponse:
        return JSONResponse({"detail": translate(str(exc), language(request.headers.get("accept-language")))}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = translate(exc.detail, language(request.headers.get("accept-language"))) if isinstance(exc.detail, str) else exc.detail
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError) -> JSONResponse:
        locale = language(request.headers.get("accept-language"))
        return JSONResponse({"detail": [validation_detail(error, locale) for error in exc.errors()]}, status_code=422)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        return templates.TemplateResponse(request=request, name="index.html", context={"translation_catalog": CATALOG})

    async def context(bot_id: str, request: Request):
        async with manager.lock(bot_id):
            auth.bot_access(request, bot_id)
            profile = manager.profile(bot_id)
            if not profile:
                raise HTTPException(404, "Профиль бота не найден")
            yield profile

    async def selected_database(bot_id: str, request: Request):
        auth.bot_access(request, bot_id)
        if not manager.profile(bot_id):
            raise HTTPException(404, "Профиль бота не найден")
        return database.scoped(bot_id)

    async def selected_service(profile=Depends(context)):
        service = manager.services.get(profile["id"])
        if not service:
            raise HTTPException(409, "Бот отключён или отсутствует токен. Настройте профиль и env, затем перезапустите приложение.")
        return service

    @app.get("/api/bots")
    async def bot_profiles(request: Request):
        locale = language(request.headers.get("accept-language"))
        result = []
        for profile in database.profiles():
            role = auth.access_role(request.state.identity, profile["id"])
            if not role:
                continue
            public_profile = profile if request.state.identity.owner else {key: profile[key] for key in ("id", "name", "enabled")}
            result.append({**public_profile, "access_role": role, "status": localized_status(manager.status(profile), locale)})
        return result

    @app.post("/api/bots", status_code=201, dependencies=[Depends(auth.owner)])
    async def add_bot(data: BotProfile):
        async with manager.lock(data.id):
            if manager.profile(data.id):
                raise HTTPException(409, "Профиль уже существует")
            database.save_profile(data.model_dump())
            database.scoped(data.id).audit("profile_saved", {"id": data.id})
            await manager.restart(data.id)
        return {"id": data.id}

    @app.put("/api/bots/{bot_id}", dependencies=[Depends(auth.owner)])
    async def update_bot(bot_id: str, data: BotProfile):
        async with manager.lock(bot_id):
            if not manager.profile(bot_id):
                raise HTTPException(404, "Профиль не найден")
            if bot_id != data.id:
                raise HTTPException(400, "Внутренний ID профиля менять нельзя")
            database.save_profile(data.model_dump())
            database.scoped(bot_id).audit("profile_saved", {"id": bot_id})
            await manager.restart(bot_id)
        return {"id": bot_id}

    @app.post("/api/bots/{bot_id}/reconnect", dependencies=[Depends(auth.owner)])
    async def reconnect(bot_id: str, request: Request, profile=Depends(context)):
        await manager.restart(bot_id)
        return localized_status(manager.status(profile), language(request.headers.get("accept-language")))

    @app.get("/api/bots/{bot_id}/status")
    async def status(request: Request, profile=Depends(context)) -> dict:
        return localized_status(manager.status(profile), language(request.headers.get("accept-language")))

    @app.get("/api/bots/{bot_id}/guilds")
    async def guilds(discord_service=Depends(selected_service)) -> list[dict]:
        return discord_service.list_guilds()

    @app.get("/api/bots/{bot_id}/guilds/{guild_id}/channels")
    async def channels(guild_id: int, discord_service=Depends(selected_service)) -> list[dict]:
        return discord_service.list_channels(guild_id)

    @app.get("/api/bots/{bot_id}/guilds/{guild_id}/roles")
    async def roles(guild_id: int, discord_service=Depends(selected_service)) -> list[dict]:
        return discord_service.list_roles(guild_id)

    @app.get("/api/emojis")
    async def emojis() -> list[dict[str, str]]:
        return unicode_emoji_catalog()

    @app.post("/api/bots/{bot_id}/uploads")
    async def upload(file: UploadFile = File(...), database=Depends(selected_database)) -> dict:
        original_name = Path(file.filename or "file").name[:255]
        upload_id = uuid.uuid4().hex
        suffix = Path(original_name).suffix[:20]
        stored_name = f"{upload_id}{suffix}"
        destination = settings.data_dir / "uploads" / stored_name
        limit = settings.max_upload_mb * 1024 * 1024
        size = 0
        try:
            with destination.open("xb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Файл больше лимита {settings.max_upload_mb} МБ",
                        )
                    output.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

        database.add_upload(upload_id, original_name, stored_name, file.content_type, size)
        database.audit("upload_created", {"upload_id": upload_id, "name": original_name, "size": size})
        return {"id": upload_id, "name": original_name, "size": size}

    @app.get("/api/bots/{bot_id}/messages")
    async def messages(database=Depends(selected_database)) -> list[dict]:
        return database.list_messages()

    @app.post("/api/bots/{bot_id}/messages/import")
    async def import_messages(data: MessageImportRequest, discord_service=Depends(selected_service)) -> dict:
        return await discord_service.import_own_messages(
            int(data.guild_id),
            int(data.channel_id) if data.channel_id else None,
            data.limit_per_channel,
        )

    @app.post("/api/bots/{bot_id}/messages", status_code=201)
    async def create_message(data: MessagePayload, discord_service=Depends(selected_service), database=Depends(selected_database)) -> dict:
        payload = data.model_dump()
        message = await discord_service.send_message(payload)
        record_id = database.add_message(
            message.guild.id, message.channel.id, message.id, payload
        )
        database.audit(
            "message_created",
            {"record_id": record_id, "channel_id": str(message.channel.id), "message_id": str(message.id)},
        )
        return {"id": record_id, "message_id": str(message.id), "jump_url": message.jump_url}

    @app.put("/api/bots/{bot_id}/messages/{record_id}")
    async def update_message(record_id: int, data: MessagePayload, discord_service=Depends(selected_service), database=Depends(selected_database)) -> dict:
        record = database.get_message(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Сообщение не найдено")
        payload = data.model_dump()
        payload["guild_id"] = record["guild_id"]
        payload["channel_id"] = record["channel_id"]
        await discord_service.edit_message(record, payload)
        database.update_message(record_id, payload)
        database.audit("message_updated", {"record_id": record_id})
        return {"ok": True}

    @app.delete("/api/bots/{bot_id}/messages/{record_id}")
    async def delete_message(record_id: int, discord_service=Depends(selected_service), database=Depends(selected_database)) -> dict:
        record = database.get_message(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Сообщение не найдено")
        await discord_service.delete_message(record)
        database.delete_reaction_roles_for_message(record["message_id"])
        database.delete_message(record_id)
        database.audit("message_deleted", {"record_id": record_id})
        return {"ok": True}

    @app.get("/api/bots/{bot_id}/reaction-roles")
    async def reaction_roles(database=Depends(selected_database)) -> list[dict]:
        return database.list_reaction_roles()

    @app.post("/api/bots/{bot_id}/reaction-roles", status_code=201)
    async def create_reaction_role(data: ReactionRoleCreate, discord_service=Depends(selected_service), database=Depends(selected_database)) -> dict:
        values = data.model_dump()
        key, display = await discord_service.create_reaction_role(values)
        try:
            record_id = database.add_reaction_role(
                data.guild_id,
                data.channel_id,
                data.message_id,
                key,
                display,
                data.role_id,
                data.remove_on_unreact,
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="Для этого сообщения и эмодзи правило уже существует",
            ) from exc
        database.audit("reaction_role_created", {"record_id": record_id})
        return {"id": record_id}

    @app.delete("/api/bots/{bot_id}/reaction-roles/{record_id}")
    async def delete_reaction_role(record_id: int, database=Depends(selected_database)) -> dict:
        record = database.get_reaction_role(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Правило не найдено")
        database.delete_reaction_role(record_id)
        database.audit("reaction_role_deleted", {"record_id": record_id})
        return {"ok": True}

    @app.get("/api/bots/{bot_id}/audit")
    async def audit(limit: int = 100, database=Depends(selected_database)) -> list[dict]:
        return database.list_audit(limit)

    return app
