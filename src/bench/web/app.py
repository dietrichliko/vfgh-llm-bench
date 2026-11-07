"""FastAPI application: server-rendered pages (Jinja + htmx), login, admin pages."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from fastapi import FastAPI, Request, status
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlmodel import Session
from starlette.middleware.sessions import SessionMiddleware

from bench import __version__, auth, prompts
from bench.activity import THROTTLE_SECONDS, ActivityMarker
from bench.db import make_engine, migrate
from bench.llm.http import make_http_client
from bench.normsets import version_of
from bench.providers import StudyConfig, load_config
from bench.ris.client import RisClient
from bench.rismcp.norms import RisClient as NormsRisClient
from bench.settings import SESSION_SECRET, Settings, get_settings
from bench.web import (
    calibration,
    cases,
    checklists,
    contamination,
    dashboard,
    opposing,
    orders,
    pages,
    study,
)
from bench.web import prompts as prompt_pages

log = logging.getLogger(__name__)
HERE = Path(__file__).parent

ROLE_LABELS = {
    "pending": "wartet auf Freischaltung",
    "viewer": "Lesezugriff",
    "expert": "Juristin / Jurist",
    "admin": "Administration",
}


def read_config(settings: Settings) -> tuple[StudyConfig | None, str | None]:
    """The app starts even with a broken config; the providers page shows the error."""
    try:
        return load_config(settings.config_path), None
    except FileNotFoundError:
        return None, f"Datei nicht gefunden: {settings.config_path}"
    except (ValidationError, ValueError) as exc:
        return None, str(exc)


def create_app(
    settings: Settings | None = None,
    *,
    http_client: httpx2.AsyncClient | None = None,
    ris: RisClient | None = None,
    norms_ris: NormsRisClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.db_url)

    marker = ActivityMarker(settings.activity_file) if settings.activity_file else None

    async def mark_busy(app: FastAPI, marker: ActivityMarker) -> None:
        """Background tasks (e.g. a case import) count as activity while they run."""
        while True:
            if app.state.tasks:
                marker.touch()
            await asyncio.sleep(THROTTLE_SECONDS)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        migrate(engine)
        with Session(engine) as db:
            if added := prompts.seed(db):
                log.info("prompts seeded: %s", ", ".join(added))
            db.commit()
        busy = asyncio.create_task(mark_busy(app, marker)) if marker else None
        yield
        if busy:
            busy.cancel()
        for task in list(app.state.tasks):
            task.cancel()
        await app.state.ris.aclose()
        await app.state.norms_ris.aclose()
        await app.state.http_client.aclose()
        engine.dispose()

    app = FastAPI(title="VfGH-LLM-Studie", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.oauth = auth.make_oauth(settings)
    app.state.config, app.state.config_error = read_config(settings)
    app.state.http_client = http_client or make_http_client()
    app.state.ris = ris or RisClient()  # case law: decisions to import (admin only)
    app.state.norms_ris = norms_ris or NormsRisClient()  # legislation: norms for condition B
    app.state.tasks = set()

    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["role_labels"] = ROLE_LABELS
    templates.env.filters["norm_version"] = version_of
    app.state.templates = templates

    session_secret = settings.secret(SESSION_SECRET)
    if session_secret is None:
        if not settings.dev_login:
            raise RuntimeError(f"secret {SESSION_SECRET} is not set")
        session_secret = "dev-insecure"  # local development only
    # Authlib keeps the OAuth state in this short-lived cookie session.
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="bench_oauth",
        https_only=settings.secure_cookies,
        max_age=600,
    )

    @app.exception_handler(auth.LoginRequired)
    async def _login_required(request: Request, _exc: auth.LoginRequired) -> Response:
        if request.headers.get("hx-request"):
            return Response(
                status_code=status.HTTP_401_UNAUTHORIZED, headers={"HX-Redirect": "/login"}
            )
        return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)

    if marker:

        @app.middleware("http")
        async def _mark_activity(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            if request.url.path != "/health":  # fly's health checks are not use
                marker.touch()
            return await call_next(request)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(cases.router)
    app.include_router(prompt_pages.router)
    app.include_router(orders.router)
    app.include_router(checklists.router)
    app.include_router(calibration.router)
    app.include_router(opposing.router)
    app.include_router(contamination.router)
    app.include_router(dashboard.router)
    app.include_router(study.router)
    return app
