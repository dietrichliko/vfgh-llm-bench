"""Prompt pages: list, history, edit (admin; the legal expert may read)."""

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from bench import prompts
from bench.auth import AdminDep, DbDep, StaffDep
from bench.web.pages import render

router = APIRouter(prefix="/prompts")


def _task_label(k: str) -> str:
    task, _, phrasing = k.partition(".p")
    return f"{prompts.TASKS.get(task, task)}, Formulierung {phrasing}"


@router.get("", response_class=HTMLResponse)
def prompt_list(request: Request, user: StaffDep, db: DbDep) -> HTMLResponse:
    return render(
        request,
        "prompts.html",
        user=user,
        prompts=prompts.all_latest(db),
        label=_task_label,
        marker=prompts.USER_MARKER,
    )


@router.get("/{key}", response_class=HTMLResponse)
def prompt_detail(
    request: Request, key: str, user: StaffDep, db: DbDep, version: int | None = None
) -> HTMLResponse:
    versions = prompts.history(db, key)
    if not versions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekannter Prompt")
    shown = next((v for v in versions if v.version == version), versions[0])
    return render(
        request,
        "prompt.html",
        user=user,
        key=key,
        label=_task_label(key),
        versions=versions,
        shown=shown,
        latest=versions[0],
        text=versions[0].text,
        problems=[],
        marker=prompts.USER_MARKER,
    )


@router.post("/{key}", response_class=HTMLResponse)
def prompt_save(
    request: Request,
    key: str,
    user: AdminDep,
    db: DbDep,
    text: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
) -> Response:
    versions = prompts.history(db, key)
    if not versions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekannter Prompt")
    try:
        prompts.save(db, key, text, user.id, note)
    except ValueError as exc:
        return render(
            request,
            "prompt.html",
            user=user,
            key=key,
            label=_task_label(key),
            versions=versions,
            shown=versions[0],
            latest=versions[0],
            text=text,
            note=note,
            problems=str(exc).split("; "),
            marker=prompts.USER_MARKER,
        )
    db.commit()
    return RedirectResponse(f"/prompts/{key}", status.HTTP_303_SEE_OTHER)
