"""Own operator Swagger, diagnostic reads, and an authorized Kafka translation entry."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import OAuth2AuthorizationCodeBearer
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from translator_service import operator_translation
from translator_service.api import app as internal_app
from translator_service.observability import instrument_api
from translator_service.operator_auth import TRANSLATE_ROLE, Settings, Verifier
from translator_service.operator_translation import TranslationInput, TranslationOutput

ASSETS = Path(__file__).resolve().parents[2] / "swagger-assets"


class TranslationBodyLimit:
    """Bound operator JSON before parsing, even without Content-Length."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] != "/v1/translate"
        ):
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > 262144:
                await JSONResponse({"detail": "request body too large"}, 413)(
                    scope, receive, send
                )
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        used = False

        async def replay() -> Message:
            nonlocal used
            if used:
                return await receive()
            used = True
            return {
                "type": "http.request",
                "body": b"".join(chunks),
                "more_body": False,
            }

        await self.app(scope, replay, send)


def create_app(
    settings: Settings | None = None,
    verifier: Verifier | None = None,
    *,
    protect_reads: bool = True,
    enable_translation: bool = True,
) -> FastAPI:
    application = FastAPI(
        title="DRP translator operations",
        docs_url=None,
        redoc_url=None,
        openapi_tags=internal_app.openapi_tags,
    )
    operations = APIRouter()
    operations.routes.extend(
        route for route in internal_app.routes if isinstance(route, APIRoute)
    )
    if settings is not None:
        verifier = verifier or Verifier(settings)
        oauth = OAuth2AuthorizationCodeBearer(
            authorizationUrl=settings.issuer + "/protocol/openid-connect/auth",
            tokenUrl=settings.issuer + "/protocol/openid-connect/token",
            scopes={"openid": "Operator sign-in"},
        )

        def authorize(token: Annotated[str, Depends(oauth)]) -> None:
            assert verifier is not None
            verifier.verify(token)

        application.include_router(
            operations, dependencies=[Depends(authorize)] if protect_reads else []
        )

        def translate_actor(token: Annotated[str, Depends(oauth)]) -> str:
            assert verifier is not None
            return verifier.verify(token, TRANSLATE_ROLE)

        def translate(
            payload: TranslationInput, actor: Annotated[str, Depends(translate_actor)]
        ) -> TranslationOutput:
            """Submit a new operator intent through Kafka and await its result.

            Return the translation only after the durable worker/outbox path and
            Kafka result ACK. Requires operator-translate, not operator-read.
            A timeout or disconnect does not cancel the queued job. Each POST is
            a new intent; no automatic HTTP retry is performed.
            """
            try:
                return operator_translation.service().translate(payload, actor)
            except operator_translation.OperatorTranslationError as error:
                raise HTTPException(
                    error.status,
                    {"error_code": error.code, "request_id": error.request_id},
                ) from error

        if enable_translation:
            application.add_api_route(
                "/v1/translate",
                translate,
                methods=["POST"],
                tags=["translator"],
                summary="Translate operator text through the selected Kafka queue",
                response_model=TranslationOutput,
                responses={
                    401: {"description": "Own access token required"},
                    403: {"description": "operator-translate permission required"},
                    413: {"description": "JSON body exceeds 262144 bytes"},
                    422: {
                        "description": "Invalid input or terminal translation failure"
                    },
                    429: {"description": "Operator concurrency limit reached"},
                    502: {"description": "Invalid or mismatched result"},
                    503: {"description": "Identity, storage, or Kafka unavailable"},
                    504: {"description": "Deadline exceeded; the job is not canceled"},
                },
            )
    else:
        application.include_router(operations)

    instrument_api(application)

    if settings is not None and enable_translation:
        application.add_middleware(TranslationBodyLimit)

    @application.middleware("http")
    async def protect_response(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        identity_origin = ""
        if settings is not None:
            parsed = urlsplit(settings.issuer)
            identity_origin = " " + parsed.scheme + "://" + parsed.netloc
        response.headers.update(
            {
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'" + identity_origin + "; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            }
        )
        return response

    @application.get("/docs", include_in_schema=False)
    def documentation() -> HTMLResponse:
        return HTMLResponse(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>DRP translator operations — Swagger</title>"
            '<link rel="stylesheet" href="/docs/assets/swagger-ui.css"></head>'
            '<body><div id="swagger-ui"></div>'
            '<script src="/docs/assets/swagger-ui-bundle.js"></script>'
            '<script src="/docs/init.js"></script></body></html>'
        )

    @application.get("/docs/init.js", include_in_schema=False)
    def initialize_documentation() -> Response:
        setup = 'window.ui=SwaggerUIBundle({url:"/openapi.json",dom_id:"#swagger-ui",'
        setup += "deepLinking:true,validatorUrl:null,persistAuthorization:false,"
        setup += (
            'oauth2RedirectUrl:window.location.origin+"/docs/oauth2-redirect.html"});'
        )
        if settings is not None:
            setup += (
                "window.ui.initOAuth("
                + json.dumps(
                    {
                        "clientId": settings.client_id,
                        "scopes": ["openid"],
                        "usePkceWithAuthorizationCodeGrant": True,
                    }
                )
                + ");"
            )
        return Response(setup, media_type="application/javascript")

    @application.get("/docs/assets/swagger-ui-bundle.js", include_in_schema=False)
    def javascript() -> FileResponse:
        return FileResponse(
            ASSETS / "swagger-ui-bundle.js", media_type="text/javascript"
        )

    @application.get("/docs/assets/swagger-ui.css", include_in_schema=False)
    def stylesheet() -> FileResponse:
        return FileResponse(ASSETS / "swagger-ui.css", media_type="text/css")

    if settings is not None:

        @application.get("/docs/oauth2-redirect.html", include_in_schema=False)
        def callback() -> HTMLResponse:
            return HTMLResponse(
                '<!doctype html><html><head><meta charset="utf-8">'
                "<title>Authorization</title></head><body>"
                '<script src="/docs/oauth2-redirect.js"></script></body></html>'
            )

        @application.get("/docs/oauth2-redirect.js", include_in_schema=False)
        def redirect_script() -> FileResponse:
            return FileResponse(
                ASSETS / "oauth2-redirect.js", media_type="text/javascript"
            )

    return application


def local_app() -> FastAPI:
    enabled = operator_translation.enabled()
    if enabled:
        operator_translation.Settings.environment()
    return create_app(Settings.environment(), enable_translation=enabled)


def network_app() -> FastAPI:
    """mTLS protects diagnostic reads; translation additionally requires own OIDC."""
    if not operator_translation.enabled():
        return create_app()
    operator_translation.Settings.environment()
    return create_app(Settings.environment(), protect_reads=False)


app = create_app()
