"""The print agent's idle polling must not keep the database busy."""
import pytest
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.models.print_agent import PrintAgent
from app.models.print_job import PrintJob
from app.services import agent_idle_cache


@pytest.fixture(autouse=True)
def _fresh_cache():
    agent_idle_cache.forget()


@pytest.fixture
def statements():
    seen = []

    def record(conn, cursor, statement, *args):
        seen.append(statement.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


def _agent(client, register):
    headers, _ = register()
    created = client.post("/print-agents", headers=headers, json={"name": "Drucker"})
    assert created.status_code in (200, 201), created.text
    data = created.json()
    return headers, data["id"], {"Authorization": f"Bearer {data['api_key']}"}


def _poll(client, agent_id, auth):
    return client.get(f"/print-agents/{agent_id}/next-job", headers=auth)


def test_idle_polls_after_the_first_do_not_touch_the_database(client, register, statements):
    _, agent_id, auth = _agent(client, register)
    assert _poll(client, agent_id, auth).status_code == 204
    statements.clear()
    for _ in range(20):
        assert _poll(client, agent_id, auth).status_code == 204
    assert statements == []


def test_a_new_job_is_delivered_at_once(client, register):
    headers, agent_id, auth = _agent(client, register)
    assert _poll(client, agent_id, auth).status_code == 204
    r = client.post("/print-jobs/test", headers=headers,
                    json={"print_agent_id": agent_id, "label_pdf_url": "https://example.com/l.pdf"})
    assert r.status_code == 201, r.text
    response = _poll(client, agent_id, auth)
    assert response.status_code == 200 and response.json()["id"] == r.json()["id"]


def test_a_wrong_key_never_benefits_from_the_cache(client, register):
    _, agent_id, auth = _agent(client, register)
    assert _poll(client, agent_id, auth).status_code == 204
    assert _poll(client, agent_id, {"Authorization": "Bearer wrong"}).status_code == 401
    assert _poll(client, agent_id, {"Authorization": "nonsense"}).status_code == 401


def test_disabling_the_agent_takes_effect_at_once(client, register):
    _, agent_id, auth = _agent(client, register)
    assert _poll(client, agent_id, auth).status_code == 204
    with SessionLocal() as db:
        db.get(PrintAgent, agent_id).status = "disabled"
        db.commit()
    assert _poll(client, agent_id, auth).status_code == 403


def test_the_database_is_asked_again_after_the_time_limit(client, register, statements, monkeypatch):
    _, agent_id, auth = _agent(client, register)
    monkeypatch.setattr(agent_idle_cache, "IDLE_SECONDS", -1)
    assert _poll(client, agent_id, auth).status_code == 204
    statements.clear()
    assert _poll(client, agent_id, auth).status_code == 204
    assert "SELECT" in statements
    assert "UPDATE" not in statements  # the last-seen time was written moments ago


def test_a_change_during_the_lookup_is_not_cached_over():
    before = agent_idle_cache.generation()
    agent_idle_cache.forget()
    agent_idle_cache.remember("a", "k", before)
    assert not agent_idle_cache.is_idle("a", "k")
