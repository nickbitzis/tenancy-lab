"""Red-team suite: try to read or write another tenant's data through every endpoint.

For one backend it creates a victim and an attacker tenant on the same pattern, plants a canary string in
every victim table, then fires each attack as the attacker. An attack leaks if the response contains the
canary or any victim id, if a request that must be denied succeeds, or if the victim's checksums change.

Run: tenancylab redteam --backend all            (exit 1 on any leak)
     tenancylab redteam --backend pooled --planted-bug --expect-leaks   (exit 1 if the bug is NOT caught)
"""

from __future__ import annotations

import base64
import json
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .auth import issue_token
from .backends import PATTERNS

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

CANARY = "CANARY-VICTIM-7f3a"
SECRET = "redteam-secret-not-used-anywhere-else"
DOMAIN = "tenancylab.test"
VICTIM, ATTACKER = "victim-co", "attacker-co"

# (name, category). Kept as a constant so pytest can parametrize at collection time.
ATTACKS = [
    ("forged_tenant_header", "tenant resolution"),
    ("forged_subdomain", "tenant resolution"),
    ("header_subdomain_mismatch", "tenant resolution"),
    ("tampered_token_claim", "tenant resolution"),
    ("unsigned_token", "tenant resolution"),
    ("no_token", "tenant resolution"),
    ("idor_get_project", "IDOR read"),
    ("idor_get_task", "IDOR read"),
    ("idor_list_project_tasks", "IDOR read"),
    ("idor_list_comments", "IDOR read"),
    ("idor_list_attachments", "IDOR read"),
    ("idor_patch_task", "IDOR write"),
    ("idor_patch_project", "IDOR write"),
    ("idor_delete_project", "IDOR write"),
    ("idor_post_comment", "IDOR write"),
    ("idor_post_attachment", "IDOR write"),
    ("idor_post_task_into_project", "IDOR write"),
    ("body_tenant_id_injection", "IDOR write"),
    ("list_projects", "listing"),
    ("list_users", "listing"),
    ("bulk_export", "bulk export"),
    ("search_canary", "search"),
    ("search_wildcard", "search"),
    ("report_summary", "reports"),
    ("background_job_result", "background jobs"),
    ("background_job_idor", "background jobs"),
    ("victim_data_unchanged", "integrity"),
]
ATTACK_NAMES = [a for a, _ in ATTACKS]
DENY = {400, 401, 403, 404}


def _b64(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode()


def _hdr(token: str | None, tenant: str | None = None, host: str | None = None) -> dict:
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if tenant:
        h["X-Tenant-ID"] = tenant
    if host:
        h["Host"] = host
    return h


class Ctx:
    def __init__(
        self,
        client: TestClient,
        victim_ids: set[str],
        victim_job: str,
        victim_project: str,
        victim_task: str,
        att_tok: str,
        vic_tok: str,
    ):
        self.c, self.victim_ids, self.victim_job = client, victim_ids, victim_job
        self.vp, self.vt, self.att_tok, self.vic_tok = victim_project, victim_task, att_tok, vic_tok

    def att(self) -> dict:
        return _hdr(self.att_tok, ATTACKER)


def _leaks(ctx: Ctx, text: str) -> list[str]:
    found = []
    if CANARY in text:
        found.append("canary")
    found += sorted(i for i in ctx.victim_ids if i in text)
    return found


def _setup(client: TestClient, platform, pattern: str) -> Ctx:
    platform.onboard(VICTIM, "Victim Co", pattern)
    platform.onboard(ATTACKER, "Attacker Co", pattern)
    vic_tok = issue_token(SECRET, VICTIM, "usr_victim")
    att_tok = issue_token(SECRET, ATTACKER, "usr_attacker")
    vh, ah = _hdr(vic_tok, VICTIM), _hdr(att_tok, ATTACKER)
    vrepo = platform.repo(VICTIM)
    u = vrepo.create_user(f"{CANARY.lower()}@victim.test", f"{CANARY} owner")
    p = client.post("/api/projects", json={"name": f"{CANARY} roadmap", "description": CANARY}, headers=vh).json()
    t = client.post(
        f"/api/projects/{p['id']}/tasks", json={"title": f"{CANARY} merger plan", "body": CANARY}, headers=vh
    ).json()
    cm = client.post(f"/api/tasks/{t['id']}/comments", json={"body": f"{CANARY} board notes"}, headers=vh).json()
    at = client.post(
        f"/api/tasks/{t['id']}/attachments",
        headers=vh,
        json={"filename": f"{CANARY}.pdf", "content_type": "application/pdf", "size_bytes": 10, "sha256": "a" * 64},
    ).json()
    job = client.post("/api/jobs/report", headers=vh).json()
    platform.jobs.wait(job["id"])
    # The attacker has ordinary data of its own, with ordinary words that also appear in victim rows.
    ap = client.post("/api/projects", json={"name": "Attacker roadmap"}, headers=ah).json()
    client.post(f"/api/projects/{ap['id']}/tasks", json={"title": "Attacker merger plan"}, headers=ah)
    ids = {u["id"], p["id"], t["id"], cm["id"], at["id"]}
    return Ctx(client, ids, job["id"], p["id"], t["id"], att_tok, vic_tok)


def _attack(ctx: Ctx, name: str):
    c, ah = ctx.c, ctx.att()
    if name == "forged_tenant_header":
        return "deny", c.get("/api/projects", headers=_hdr(ctx.att_tok, VICTIM))
    if name == "forged_subdomain":
        return "deny", c.get("/api/projects", headers=_hdr(ctx.att_tok, host=f"{VICTIM}.{DOMAIN}"))
    if name == "header_subdomain_mismatch":
        return "deny", c.get("/api/projects", headers=_hdr(ctx.att_tok, VICTIM, f"{ATTACKER}.{DOMAIN}"))
    if name == "tampered_token_claim":
        body, sig = ctx.att_tok.split(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        claims["tid"] = VICTIM
        forged = _b64(json.dumps(claims, separators=(",", ":")).encode()) + "." + sig
        return "deny", c.get("/api/projects", headers=_hdr(forged, VICTIM))
    if name == "unsigned_token":
        body = _b64(json.dumps({"tid": VICTIM, "sub": "x", "exp": int(time.time()) + 60}).encode())
        return "deny", c.get("/api/projects", headers=_hdr(body + ".", VICTIM))
    if name == "no_token":
        return "deny", c.get("/api/projects", headers=_hdr(None, VICTIM))
    if name == "idor_get_project":
        return "deny", c.get(f"/api/projects/{ctx.vp}", headers=ah)
    if name == "idor_get_task":
        return "deny", c.get(f"/api/tasks/{ctx.vt}", headers=ah)
    if name == "idor_list_project_tasks":
        return "deny", c.get(f"/api/projects/{ctx.vp}/tasks", headers=ah)
    if name == "idor_list_comments":
        return "deny", c.get(f"/api/tasks/{ctx.vt}/comments", headers=ah)
    if name == "idor_list_attachments":
        return "deny", c.get(f"/api/tasks/{ctx.vt}/attachments", headers=ah)
    if name == "idor_patch_task":
        return "deny", c.patch(f"/api/tasks/{ctx.vt}", json={"title": "pwned", "status": "done"}, headers=ah)
    if name == "idor_patch_project":
        return "deny", c.patch(f"/api/projects/{ctx.vp}", json={"name": "pwned"}, headers=ah)
    if name == "idor_delete_project":
        return "deny", c.delete(f"/api/projects/{ctx.vp}", headers=ah)
    if name == "idor_post_comment":
        return "deny", c.post(f"/api/tasks/{ctx.vt}/comments", json={"body": "pwned"}, headers=ah)
    if name == "idor_post_attachment":
        return "deny", c.post(
            f"/api/tasks/{ctx.vt}/attachments",
            headers=ah,
            json={"filename": "x.exe", "size_bytes": 1, "sha256": "b" * 64},
        )
    if name == "idor_post_task_into_project":
        return "deny", c.post(f"/api/projects/{ctx.vp}/tasks", json={"title": "pwned"}, headers=ah)
    if name == "body_tenant_id_injection":
        return "allow", c.post("/api/projects", json={"name": "injected", "tenant_id": VICTIM}, headers=ah)
    if name == "list_projects":
        return "allow", c.get("/api/projects", headers=ah)
    if name == "list_users":
        return "allow", c.get("/api/users", headers=ah)
    if name == "bulk_export":
        return "allow", c.get("/api/export", headers=ah)
    if name == "search_canary":
        return "allow", c.get("/api/search", params={"q": CANARY}, headers=ah)
    if name == "search_wildcard":
        return "allow", c.get("/api/search", params={"q": "merger"}, headers=ah)
    if name == "report_summary":
        return "allow", c.get("/api/reports/summary", headers=ah)
    if name == "background_job_result":
        job = c.post("/api/jobs/report", headers=ah).json()
        c.app.state.platform.jobs.wait(job["id"])
        return "allow", c.get(f"/api/jobs/{job['id']}", headers=ah)
    if name == "background_job_idor":
        return "deny", c.get(f"/api/jobs/{ctx.victim_job}", headers=ah)
    raise KeyError(name)


def run_backend(pattern: str, planted_bug: bool = False) -> dict:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    from .app import Settings, create_app

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            data_dir=Path(tmp), secret=SECRET, admin_key="redteam-admin", planted_bug=planted_bug, limits=False
        )
        app = create_app(settings)
        with TestClient(app, base_url=f"http://api.{DOMAIN}") as client:
            plat = app.state.platform
            ctx = _setup(client, plat, pattern)
            before = plat.repo(VICTIM).checksums()["all"]
            for name, category in ATTACKS:
                if name == "victim_data_unchanged":
                    after = plat.repo(VICTIM).checksums()["all"]
                    leaked = after != before
                    results.append(
                        {
                            "attack": name,
                            "category": category,
                            "status": None,
                            "leaked": leaked,
                            "detail": "victim checksums changed" if leaked else "victim checksums identical",
                        }
                    )
                    continue
                expect, resp = _attack(ctx, name)
                found = _leaks(ctx, resp.text)
                reasons = [f"response contains {', '.join(found)}"] if found else []
                if expect == "deny" and resp.status_code not in DENY:
                    reasons.append(f"expected denial, got {resp.status_code}")
                if expect == "allow" and resp.status_code >= 400:
                    reasons.append(f"attacker's own request failed with {resp.status_code}")
                results.append(
                    {
                        "attack": name,
                        "category": category,
                        "status": resp.status_code,
                        "leaked": bool(found) or (expect == "deny" and resp.status_code not in DENY),
                        "detail": "; ".join(reasons) or f"{resp.status_code}, nothing of the victim's",
                    }
                )
    leaks = [r for r in results if r["leaked"]]
    return {
        "backend": pattern,
        "planted_bug": planted_bug,
        "attacks": len(results),
        "leaks": len(leaks),
        "leaked_attacks": [r["attack"] for r in leaks],
        "results": results,
    }


def run_all(patterns=PATTERNS, with_planted_bug: bool = True) -> dict:
    runs = [run_backend(p) for p in patterns]
    if with_planted_bug:
        runs.append(run_backend("pooled", planted_bug=True))
    return {"canary": CANARY, "runs": runs}
