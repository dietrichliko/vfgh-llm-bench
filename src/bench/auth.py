"""Login via any OIDC provider (e.g. CERN SSO) or, locally, a dev login; server-side sessions.

The app assigns roles itself: new accounts are ``pending`` until an admin activates them.
Addresses in ``BENCH_ADMIN_EMAILS`` become admins directly.
"""

import hashlib
import secrets
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session, col, delete, select

from bench.db import get_session
from bench.models import User, UserRole, UserSession, audit, utcnow
from bench.settings import OIDC_CLIENT_SECRET, Settings

COOKIE = "bench_session"
OIDC = "oidc"

router = APIRouter(prefix="/auth", tags=["auth"])


def make_oauth(settings: Settings) -> OAuth:
    oauth = OAuth()
    if settings.oidc_issuer and settings.oidc_client_id:
        oauth.register(
            OIDC,
            client_id=settings.oidc_client_id,
            client_secret=settings.secret(OIDC_CLIENT_SECRET),
            server_metadata_url=f"{settings.oidc_issuer}/.well-known/openid-configuration",
            client_kwargs={"scope": "openid profile email"},
        )
    return oauth


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def upsert_user(db: Session, settings: Settings, *, sub: str, email: str, name: str) -> User:
    email = email.strip().lower()
    name = name.strip() or email
    user = db.exec(select(User).where(User.sub == sub)).first()
    is_admin = email in settings.admin_emails
    if user is None:
        user = User(
            sub=sub, email=email, name=name, role=UserRole.ADMIN if is_admin else UserRole.PENDING
        )
        db.add(user)
        db.flush()
        audit(db, "user.created", user_id=user.id, obj=f"user:{user.id}", detail={"email": email})
    else:
        user.email, user.name = email, name
        if is_admin and user.role == UserRole.PENDING:
            user.role = UserRole.ADMIN
    user.last_login_at = utcnow()
    return user


def start_session(db: Session, settings: Settings, user: User, response: Response) -> None:
    assert user.id is not None
    token = secrets.token_urlsafe(32)
    expires = utcnow() + timedelta(hours=settings.session_hours)
    db.add(UserSession(token=_hash(token), user_id=user.id, expires_at=expires))
    audit(db, "login", user_id=user.id)
    db.commit()
    response.set_cookie(
        COOKIE,
        token,
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
    )


def _settings(request: Request) -> Settings:
    s: Settings = request.app.state.settings
    return s


DbDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(_settings)]


class LoginRequired(Exception):
    """Raised for page requests without a session; the app redirects to the login page."""


def optional_user(request: Request, db: DbDep) -> User | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = db.get(UserSession, _hash(token))
    if row is None or row.expires_at <= utcnow():
        return None
    return db.get(User, row.user_id)


def current_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    if user is None:
        raise LoginRequired
    return user


UserDep = Annotated[User, Depends(current_user)]


def require_roles(*roles: UserRole) -> Callable[..., User]:
    def dep(user: UserDep) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Keine Berechtigung")
        return user

    return dep


AdminDep = Annotated[User, Depends(require_roles(UserRole.ADMIN))]
StaffDep = Annotated[User, Depends(require_roles(UserRole.ADMIN, UserRole.EXPERT))]


@router.get("/login")
async def login(request: Request, settings: SettingsDep) -> Any:
    client = request.app.state.oauth.create_client(OIDC)
    if client is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "SSO ist nicht konfiguriert")
    return await client.authorize_redirect(request, f"{settings.public_url}/auth/callback")


@router.get("/callback")
async def callback(request: Request, db: DbDep, settings: SettingsDep) -> Response:
    client = request.app.state.oauth.create_client(OIDC)
    if client is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "SSO ist nicht konfiguriert")
    token = await client.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = info.get("email") or ""
    if not info.get("sub") or not email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Anmeldung ohne E-Mail-Adresse")
    name = info.get("name") or info.get("preferred_username") or email
    user = upsert_user(db, settings, sub=info["sub"], email=email, name=name)
    response = RedirectResponse("/", status.HTTP_303_SEE_OTHER)
    start_session(db, settings, user, response)
    return response


@router.post("/dev-login")
def dev_login(
    db: DbDep,
    settings: SettingsDep,
    email: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
) -> Response:
    if not settings.dev_login:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    email = email.strip()
    user = upsert_user(db, settings, sub=f"dev:{email.lower()}", email=email, name=name or email)
    response = RedirectResponse("/", status.HTTP_303_SEE_OTHER)
    start_session(db, settings, user, response)
    return response


@router.post("/logout")
def logout(request: Request, db: DbDep) -> Response:
    token = request.cookies.get(COOKIE)
    if token:
        db.exec(delete(UserSession).where(col(UserSession.token) == _hash(token)))
        db.commit()
    response = RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE)
    return response
