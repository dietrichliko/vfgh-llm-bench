"""Study page (admin): the draft setup, Freeze protocol, amendments, Publish results."""

from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, col, select

from bench import protocol, studyrepo
from bench.auth import AdminDep, DbDep, SettingsDep
from bench.models import (
    Case,
    CaseStatus,
    Order,
    ProtocolVersion,
    RepoPush,
    ResultsRelease,
    Study,
)
from bench.providers import Role
from bench.settings import Settings
from bench.web.pages import render, resolved_models

router = APIRouter(prefix="/study")


def _back(problem: str = "", notice: str = "") -> Response:
    url = "/study"
    if problem:
        url += f"?problem={quote(problem)}"
    elif notice:
        url += f"?notice={quote(notice)}"
    return RedirectResponse(url, status.HTTP_303_SEE_OTHER)


def _releases_dir(settings: Settings) -> Path:
    return settings.data_dir / "releases"


def _push(
    db: Session,
    settings: Settings,
    s: Study,
    tag: str,
    files: dict[str, str | bytes],
    message: str,
    replace: str | None = None,
) -> RepoPush:
    """Commit and tag in the study repository, and record the outcome."""
    try:
        commit = studyrepo.push(settings, s.slug, files, tag, message, replace)
        push = RepoPush(study_id=s.id or 0, tag=tag, ok=True, commit=commit)
    except studyrepo.RepoError as exc:
        push = RepoPush(study_id=s.id or 0, tag=tag, ok=False, message=str(exc))
    db.add(push)
    return push


@router.get("", response_class=HTMLResponse)
def page(
    request: Request,
    user: AdminDep,
    db: DbDep,
    settings: SettingsDep,
    problem: str = "",
    notice: str = "",
) -> HTMLResponse:
    s = protocol.study(db)
    models = resolved_models(request)
    context: dict[str, Any] = {
        "user": user,
        "study": s,
        "problem": problem,
        "notice": notice,
        "repo": settings.study_repo_url,
    }
    if s is not None:
        vs = protocol.versions(db, s.id or 0)
        pushes: dict[str, RepoPush] = {}
        for p in db.exec(
            select(RepoPush).where(RepoPush.study_id == s.id).order_by(col(RepoPush.id))
        ).all():
            pushes[p.tag] = p
        conf = protocol.conformance(db)
        orders = {o.id: o for o in db.exec(select(Order)).all()}
        context.update(
            versions=vs,
            pushes=pushes,
            resolved=protocol.resolve(db, s, models),
            draft=s.config or {},
            cases=db.exec(
                select(Case)
                .where(Case.status == CaseStatus.RELEASED)
                .order_by(col(Case.decision_date))
            ).all(),
            roles={
                role: [rm for rm in models.values() if Role(role) in rm.config.roles]
                for role in protocol.ROLES
            },
            conformance=conf,
            orders=orders,
            releases=db.exec(
                select(ResultsRelease)
                .where(ResultsRelease.study_id == s.id)
                .order_by(col(ResultsRelease.version))
            ).all(),
        )
    return render(request, "study.html", **context)


@router.post("/create")
def create(
    user: AdminDep,
    db: DbDep,
    slug: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
) -> Response:
    try:
        protocol.create_study(db, slug, name, user.id)
    except ValueError as exc:
        db.rollback()
        return _back(str(exc))
    db.commit()
    return _back()


@router.post("/draft")
async def draft(request: Request, user: AdminDep, db: DbDep) -> Response:
    s = protocol.study(db)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    form = await request.form()

    def ints(name: str, default: int) -> int:
        value = form.get(name)
        try:
            return int(str(value)) if value not in (None, "") else default
        except ValueError:
            return default

    conditions = {
        c: {"repetitions": max(1, ints(f"rep_{c}", 1)), "phrasings": max(1, ints(f"phr_{c}", 1))}
        for c in protocol.CONDITIONS
        if form.get(f"cond_{c}")
    }
    config = {
        "cases": sorted(int(str(v)) for v in form.getlist("cases")),
        "models": {
            role: sorted(str(v) for v in form.getlist(f"m_{role}")) for role in protocol.ROLES
        },
        "conditions": conditions,
        "cutoff_months": max(0, ints("cutoff_months", 3)),
        "eligibility": str(form.get("eligibility") or "cutoff"),
    }
    protocol.save_draft(db, s, config, user.id)
    db.commit()
    return _back(notice="Entwurf gespeichert")


@router.post("/freeze")
def freeze(
    request: Request,
    user: AdminDep,
    db: DbDep,
    settings: SettingsDep,
    reason: Annotated[str, Form()] = "",
) -> Response:
    s = protocol.study(db)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        pv = protocol.freeze(db, s, resolved_models(request), settings, user.id, reason)
    except ValueError as exc:
        db.rollback()
        return _back(str(exc))
    db.commit()
    if not studyrepo.configured(settings):
        return _back(notice=f"{pv.tag} eingefroren; bitte die ZIP-Datei von Hand committen")
    push = _push(db, settings, s, pv.tag, dict(pv.files), f"Protokoll {pv.tag}")
    db.commit()
    if not push.ok:
        return _back(f"{pv.tag} eingefroren, aber nicht übertragen: {push.message}")
    return _back(notice=f"{pv.tag} eingefroren und übertragen")


def _version(db: Session, version: int) -> tuple[Study, ProtocolVersion]:
    s = protocol.study(db)
    pv = (
        db.exec(
            select(ProtocolVersion).where(
                ProtocolVersion.study_id == s.id, ProtocolVersion.version == version
            )
        ).first()
        if s
        else None
    )
    if s is None or pv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return s, pv


@router.get("/protocol/{version}.zip")
def protocol_zip(user: AdminDep, db: DbDep, version: int) -> Response:
    s, pv = _version(db, version)
    content = protocol.zip_files(f"studies/{s.slug}/", dict(pv.files), pv.created_at)
    return Response(
        content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{pv.tag}.zip"'},
    )


@router.post("/protocol/{version}/push")
def protocol_push(user: AdminDep, db: DbDep, settings: SettingsDep, version: int) -> Response:
    s, pv = _version(db, version)
    push = _push(db, settings, s, pv.tag, dict(pv.files), f"Protokoll {pv.tag}")
    db.commit()
    return _back(push.message or "") if not push.ok else _back(notice=f"{pv.tag} übertragen")


@router.post("/publish")
def publish(
    request: Request,
    user: AdminDep,
    db: DbDep,
    settings: SettingsDep,
    raw: Annotated[bool, Form()] = False,
) -> Response:
    s = protocol.study(db)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        release, content = protocol.publish(db, resolved_models(request), user.id, raw)
    except ValueError as exc:
        db.rollback()
        return _back(str(exc))
    folder = _releases_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{release.tag}.zip").write_bytes(content)
    db.commit()
    if not studyrepo.configured(settings):
        return _back(notice=f"{release.tag} erstellt; bitte die ZIP-Datei von Hand committen")
    push = _push(
        db,
        settings,
        s,
        release.tag,
        dict(protocol.results_files(content)),
        f"Ergebnisse {release.tag}",
        replace="results",
    )
    db.commit()
    if not push.ok:
        return _back(f"{release.tag} erstellt, aber nicht übertragen: {push.message}")
    return _back(notice=f"{release.tag} veröffentlicht")


@router.get("/results/{version}.zip")
def results_zip(user: AdminDep, db: DbDep, settings: SettingsDep, version: int) -> Response:
    s = protocol.study(db)
    release = (
        db.exec(
            select(ResultsRelease).where(
                ResultsRelease.study_id == s.id, ResultsRelease.version == version
            )
        ).first()
        if s
        else None
    )
    path = _releases_dir(settings) / f"{release.tag}.zip" if release else None
    if release is None or path is None or not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return Response(
        path.read_bytes(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{release.tag}.zip"'},
    )
