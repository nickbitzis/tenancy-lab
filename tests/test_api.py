import pytest
from fastapi.testclient import TestClient

from tenancylab.app import Settings, create_app
from tenancylab.auth import issue_token

SECRET, ADMIN = "api-test-secret", "api-test-admin"


@pytest.fixture
def client(tmp_path):
    s = Settings(data_dir=tmp_path / "data", secret=SECRET, admin_key=ADMIN, results_dir=tmp_path / "results")
    with TestClient(create_app(s)) as c:
        yield c


def h(tenant, **extra):
    return {"Authorization": "Bearer " + issue_token(SECRET, tenant, "usr_1"), "X-Tenant-ID": tenant, **extra}


def onboard(c, tid, pattern="pooled", tier="starter"):
    r = c.post(
        "/admin/tenants",
        json={"id": tid, "name": tid.title(), "pattern": pattern, "tier": tier},
        headers={"X-Admin-Key": ADMIN},
    )
    assert r.status_code == 201, r.text


def test_health_and_index(client):
    assert client.get("/healthz").json()["status"] == "ok"
    r = client.get("/")
    assert r.status_code == 200 and "Tenancy Lab" in r.text


def test_admin_mutations_need_key(client):
    assert client.post("/admin/tenants", json={"id": "acme", "name": "A"}).status_code == 401
    assert (
        client.post("/admin/tenants", json={"id": "acme", "name": "A"}, headers={"X-Admin-Key": "wrong"}).status_code
        == 401
    )
    assert client.delete("/admin/tenants/acme").status_code == 401


@pytest.mark.parametrize("pattern", ["pooled", "bridge", "silo"])
def test_project_flow_end_to_end(client, pattern):
    onboard(client, "acme", pattern)
    p = client.post("/api/projects", json={"name": "Launch"}, headers=h("acme")).json()
    t = client.post(
        f"/api/projects/{p['id']}/tasks", json={"title": "Write docs", "priority": 1}, headers=h("acme")
    ).json()
    assert t["priority"] == 1
    assert client.patch(f"/api/tasks/{t['id']}", json={"status": "done"}, headers=h("acme")).json()["status"] == "done"
    client.post(f"/api/tasks/{t['id']}/comments", json={"body": "shipped"}, headers=h("acme"))
    assert len(client.get(f"/api/tasks/{t['id']}/comments", headers=h("acme")).json()) == 1
    assert client.get("/api/search", params={"q": "docs"}, headers=h("acme")).json()["tasks"][0]["id"] == t["id"]
    job = client.post("/api/jobs/report", headers=h("acme")).json()
    client.app.state.platform.jobs.wait(job["id"])
    assert client.get(f"/api/jobs/{job['id']}", headers=h("acme")).json()["status"] == "done"
    assert client.get("/api/me", headers=h("acme")).json()["pattern"] == pattern
    assert client.delete(f"/api/projects/{p['id']}", headers=h("acme")).status_code == 204


def test_subdomain_routing(client):
    onboard(client, "acme")
    tok = {"Authorization": "Bearer " + issue_token(SECRET, "acme", "u")}
    r = client.get("/api/projects", headers={**tok, "Host": "acme.tenancylab.test"})
    assert r.status_code == 200


def test_unknown_tenant_is_404(client):
    assert client.get("/api/projects", headers=h("ghost")).status_code == 404


def test_rate_limit_returns_429(client):
    onboard(client, "acme")
    codes = [client.get("/api/reports/summary", headers=h("acme")).status_code for _ in range(12)]
    assert 429 in codes  # starter burst is 100 tokens; a report costs 10
    r = client.get("/api/reports/summary", headers=h("acme"))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert client.get("/api/reports/summary", headers=h("other")).status_code == 404
    assert 'tenancylab_rate_limited_total{tenant="acme",reason="rate limit"}' in client.get("/metrics").text


def test_metrics_per_tenant(client):
    onboard(client, "acme")
    onboard(client, "bolt", "silo")
    client.get("/api/projects", headers=h("acme"))
    client.get("/api/projects", headers=h("bolt"))
    text = client.get("/metrics").text
    assert 'tenancylab_requests_total{tenant="acme",route="/api/projects",status="200"} 1' in text
    assert 'tenancylab_request_seconds_count{tenant="bolt"} 1' in text
    assert 'tenancylab_tenants{pattern="silo"} 1' in text


def test_admin_ops_over_http(client):
    onboard(client, "acme")
    client.post("/api/projects", json={"name": "Keep me"}, headers=h("acme"))
    adm = {"X-Admin-Key": ADMIN}
    b = client.post("/admin/tenants/acme/backup", headers=adm).json()
    assert client.post("/admin/tenants/acme/restore", json={"backup": b["backup"]}, headers=adm).json()["verified"]
    assert (
        client.post("/admin/tenants/acme/restore", json={"backup": "../../etc-1-abcd.json.gz"}, headers=adm).status_code
        == 422
    )
    m = client.post("/admin/tenants/acme/move", json={"to": "silo"}, headers=adm).json()
    assert m["moved"] and m["verified"]
    assert client.get("/api/me", headers=h("acme")).json()["pattern"] == "silo"
    tenants = client.get("/admin/tenants").json()
    assert tenants[0]["pattern"] == "silo" and tenants[0]["rows"] == 1
    off = client.delete("/admin/tenants/acme", headers=adm).json()
    assert off["verified"]
    assert client.get("/api/projects", headers=h("acme")).status_code == 404


def test_admin_migration_endpoints(client):
    onboard(client, "a1", "silo")
    onboard(client, "a2", "silo")
    adm = {"X-Admin-Key": ADMIN}
    r = client.post("/admin/migrations", json={"pattern": "silo"}, headers=adm).json()
    assert r["status"] == "done" and r["ddl_statements"] == 0  # already on latest
    assert client.get("/admin/migrations").json()[0]["units"] == 2
    assert client.post("/admin/migrations/999/resume", headers=adm).status_code == 404


def test_result_endpoints(client, tmp_path):
    assert client.get("/admin/noisy-neighbor").status_code == 404
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "isolation.json").write_text('{"runs": []}')
    assert client.get("/admin/isolation").json() == {"runs": []}
    assert len(client.get("/admin/cost").json()["rows"]) == 12


def test_admin_token_issue(client):
    onboard(client, "acme")
    tok = client.post("/admin/tenants/acme/token", headers={"X-Admin-Key": ADMIN}).json()["token"]
    r = client.get("/api/projects", headers={"Authorization": f"Bearer {tok}", "X-Tenant-ID": "acme"})
    assert r.status_code == 200
    assert client.post("/admin/tenants/ghost/token", headers={"X-Admin-Key": ADMIN}).status_code == 404
