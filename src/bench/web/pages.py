"""Pages: start page, login, providers and models (with test calls), users."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, col, select

from bench.auth import AdminDep, DbDep, SettingsDep, UserDep, optional_user
from bench.llm.ping import ping
from bench.models import ProviderCheck, User, UserRole, audit
from bench.providers import ResolvedModel, resolve

router = APIRouter()

OUTPUT_LABELS = {"native": "JSON-Schema", "prompted": "JSON im Prompt"}
TOOL_LABELS = {"native": "nativ", "emulated": "emuliert", "none": "keine"}
ROLE_NAMES = {
    "tested": "Prüfling",
    "judge": "Richter",
    "extractor": "Referenz",
    "writer": "Gegenbegründung",
}


def render(request: Request, name: str, **context: Any) -> HTMLResponse:
    templates = request.app.state.templates
    response: HTMLResponse = templates.TemplateResponse(request, name, context)
    return response


def resolved_models(request: Request) -> dict[str, ResolvedModel]:
    config = request.app.state.config
    return {} if config is None else resolve(config, request.app.state.settings)


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request, settings: SettingsDep, user: Annotated[User | None, Depends(optional_user)]
) -> Response:
    if user is not None:
        return RedirectResponse("/", status.HTTP_303_SEE_OTHER)
    sso = request.app.state.oauth.create_client("oidc") is not None
    return render(request, "login.html", user=None, sso=sso, dev_login=settings.dev_login)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, user: UserDep) -> HTMLResponse:
    return render(request, "home.html", user=user)


def _last_checks(db: Session) -> dict[str, ProviderCheck]:
    rows = db.exec(select(ProviderCheck).order_by(col(ProviderCheck.id).desc()).limit(500)).all()
    out: dict[str, ProviderCheck] = {}
    for row in rows:
        out.setdefault(row.model_key, row)
    return out


@router.get("/admin/providers", response_class=HTMLResponse)
def providers_page(
    request: Request, user: AdminDep, db: DbDep, settings: SettingsDep
) -> HTMLResponse:
    config = request.app.state.config
    models = resolved_models(request)
    providers = sorted(config.providers.items()) if config else []
    return render(
        request,
        "providers.html",
        user=user,
        config_path=settings.config_path,
        config_error=request.app.state.config_error,
        providers=providers,
        secrets={k: settings.has_secret(p.api_key_secret) for k, p in providers},
        models=list(models.values()),
        last_checks=_last_checks(db),
        output_labels=OUTPUT_LABELS,
        tool_labels=TOOL_LABELS,
        role_names=ROLE_NAMES,
    )


@router.post("/admin/providers/{model_key}/test", response_class=HTMLResponse)
async def test_model(
    request: Request, model_key: str, user: AdminDep, db: DbDep, settings: SettingsDep
) -> HTMLResponse:
    rm = resolved_models(request).get(model_key)
    if rm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekanntes Modell")
    if not rm.available:
        raise HTTPException(status.HTTP_409_CONFLICT, "Schlüssel fehlt")
    result = await ping(rm, settings, request.app.state.http_client)
    check = ProviderCheck(
        user_id=user.id,
        model_key=rm.key,
        model_id=rm.config.model,
        provider_key=rm.provider_key,
        config_hash=rm.config_hash,
        ok=result.ok,
        answer=result.answer,
        error=result.error,
        duration_ms=result.duration_ms,
        model_reported=result.model_reported,
        provider_reported=result.provider_reported,
        bridge_version=result.bridge_version,
        model_mismatch=result.model_mismatch,
        usage=result.usage,
        exchanges=[ex.as_dict() for ex in result.exchanges],
    )
    db.add(check)
    db.commit()
    db.refresh(check)
    return render(request, "_model_test.html", m=rm, check=check)


@router.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request, user: AdminDep, db: DbDep) -> HTMLResponse:
    users = db.exec(select(User).order_by(col(User.role), col(User.name))).all()
    return render(request, "users.html", user=user, users=users)


@router.post("/admin/users/{user_id}/role", response_class=HTMLResponse)
def set_role(
    user_id: int, role: Annotated[UserRole, Form()], user: AdminDep, db: DbDep
) -> HTMLResponse:
    if user_id == user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Die eigene Rolle ist nicht änderbar")
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    old = target.role
    target.role = role
    audit(db, "user.role", user_id=user.id, obj=f"user:{user_id}", detail={"from": old, "to": role})
    db.commit()
    return HTMLResponse("gespeichert")
