from typing import Any

import httpx2
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from bench.models import AuditLog, ProviderCheck, User, UserRole
from tests.conftest import chat_response, login


def engine(client: TestClient) -> sa.Engine:
    e: sa.Engine = client.app.state.engine  # type: ignore[attr-defined]
    return e


def test_health(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_pages_require_login(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    r = client.post("/admin/providers/judge/test", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers["hx-redirect"] == "/login"


def test_new_account_waits_for_activation(client: TestClient) -> None:
    login(client, "jurist@example.org", "Jurist")
    assert "noch nicht freigeschaltet" in client.get("/").text
    assert client.get("/admin/providers").status_code == 403


def test_admin_from_settings(client: TestClient) -> None:
    login(client, "Admin@Example.org ")
    assert "Administration" in client.get("/").text
    assert client.get("/admin/providers").status_code == 200


def test_providers_page_shows_config_and_secret_status_but_no_secret(client: TestClient) -> None:
    login(client, "admin@example.org")
    html = client.get("/admin/providers").text
    assert "claude-opus-4-8" in html and "z-ai/glm-5.3" in html
    assert "TEST_UAI_KEY" in html and "TEST_OPENROUTER_KEY" in html
    assert "Schlüssel fehlt" in html  # openrouter key is not set
    assert "nicht für Bedingung C" in html  # emulated tools on u:ai
    assert "uai-secret-value" not in html


def test_model_test_stores_check_with_raw_exchange(client: TestClient, llm: dict[str, Any]) -> None:
    llm["handler"] = lambda _req: httpx2.Response(
        200, json=chat_response(), headers={"X-Bridge-Version": "0.4.0"}
    )
    login(client, "admin@example.org")
    r = client.post("/admin/providers/judge/test")
    assert r.status_code == 200
    assert "Antwort erhalten" in r.text and "0.4.0" in r.text
    with Session(engine(client)) as db:
        check = db.exec(select(ProviderCheck)).one()
    assert check.ok and check.answer == "OK"
    assert check.model_id == "claude-opus-4-8" and len(check.config_hash) == 64
    assert check.exchanges[0]["status"] == 200
    assert "uai-secret-value" not in str(check.exchanges)
    # the page shows the last check
    assert "Antwort erhalten" in client.get("/admin/providers").text


def test_model_test_refused_without_secret(client: TestClient, llm: dict[str, Any]) -> None:
    login(client, "admin@example.org")
    assert client.post("/admin/providers/glm/test").status_code == 409
    assert client.post("/admin/providers/unknown/test").status_code == 404
    assert llm["requests"] == []


def test_admin_activates_user(client: TestClient) -> None:
    login(client, "jurist@example.org", "Jurist")
    client.cookies.clear()
    login(client, "admin@example.org")
    with Session(engine(client)) as db:
        jurist = db.exec(select(User).where(User.email == "jurist@example.org")).one()
    r = client.post(f"/admin/users/{jurist.id}/role", data={"role": "expert"})
    assert r.status_code == 200
    with Session(engine(client)) as db:
        assert db.get(User, jurist.id).role == UserRole.EXPERT  # type: ignore[union-attr]
        actions = [a.action for a in db.exec(select(AuditLog)).all()]
    assert "user.role" in actions


def test_audit_log_is_append_only(client: TestClient) -> None:
    login(client, "admin@example.org")
    with Session(engine(client)) as db, pytest.raises(sa.exc.IntegrityError):
        db.exec(sa.text("DELETE FROM audit_log"))  # type: ignore[call-overload]


def test_logout(client: TestClient) -> None:
    login(client, "admin@example.org")
    client.post("/auth/logout")
    assert client.get("/", follow_redirects=False).status_code == 303
