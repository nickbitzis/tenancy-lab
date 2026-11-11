"""FastAPI app: the tenant API (projects, tasks, comments, attachments) plus admin and ops endpoints."""

import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import cost
from .auth import AuthError, issue_token, resolve
from .backends import PATTERNS, UnknownTenant
from .limits import REPORT_COST, RateLimited, TenantLimiter
from .obs import Metrics, log_event
from .platform import MoveFailed, Platform, TenantUnavailable
from .schema import LATEST

STATIC = Path(__file__).parent / "static"
HEAVY_ROUTES = {"/api/reports/summary", "/api/jobs/report", "/api/export"}


def _default_results() -> Path:
    return Path(os.environ.get("TENANCYLAB_RESULTS", Path(__file__).resolve().parents[2] / "results"))


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("TENANCYLAB_DATA", "data")))
    secret: str = field(default_factory=lambda: os.environ.get("TENANCYLAB_SECRET") or secrets.token_hex(16))
    admin_key: str = field(default_factory=lambda: os.environ.get("TENANCYLAB_ADMIN_KEY") or secrets.token_hex(16))
    base_domain: str = field(default_factory=lambda: os.environ.get("TENANCYLAB_DOMAIN", "tenancylab.test"))
    planted_bug: bool = field(default_factory=lambda: os.environ.get("TENANCYLAB_PLANTED_BUG") == "1")
    limits: bool = True
    results_dir: Path = field(default_factory=_default_results)
    demo: bool = False


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    archived: int | None = Field(default=None, ge=0, le=1)


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=20000)
    assignee_id: str | None = None
    priority: int | None = Field(default=None, ge=1, le=4)


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=20000)
    status: str | None = Field(default=None, pattern="^(open|in_progress|done)$")
    assignee_id: str | None = None
    priority: int | None = Field(default=None, ge=1, le=4)


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class AttachmentIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=100)
    size_bytes: int = Field(ge=0, le=5_000_000_000)
    sha256: str = Field(pattern="^[0-9a-f]{64}$")


class TenantIn(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,39}$")
    name: str = Field(min_length=1, max_length=200)
    pattern: str = Field(default="pooled", pattern="^(pooled|bridge|silo)$")
    tier: str = Field(default="starter", pattern="^(starter|growth|enterprise)$")
    admin_email: str | None = None


class MigrationIn(BaseModel):
    pattern: str = Field(pattern="^(pooled|bridge|silo)$")
    target: int = Field(default=LATEST, ge=1, le=LATEST)
    fail_units: list[str] = Field(default_factory=list)


class MoveIn(BaseModel):
    to: str = Field(default="silo", pattern="^(pooled|bridge|silo)$")


class RestoreIn(BaseModel):
    backup: str = Field(pattern=r"^[a-z0-9-]+-[0-9T]+-[0-9a-f]{4}\.json\.gz$")


@dataclass
class Ctx:
    tenant_id: str
    user: str
    role: str
    platform: Platform

    @property
    def repo(self):
        try:
            return self.platform.repo(self.tenant_id)
        except TenantUnavailable as exc:
            raise HTTPException(503, "tenant is under maintenance, retry shortly") from exc


NOT_FOUND = "not found"


def _must(value, detail: str = NOT_FOUND):
    if value is None:
        raise HTTPException(404, detail)
    return value


def create_app(settings: Settings | None = None, platform: Platform | None = None) -> FastAPI:
    settings = settings or Settings()
    plat = platform or Platform(settings.data_dir, planted_bug=settings.planted_bug)
    limiter = TenantLimiter(enabled=settings.limits)
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        plat.close()

    app = FastAPI(title="Tenancy Lab", version="1.0.0", lifespan=lifespan)
    app.state.platform, app.state.settings, app.state.metrics, app.state.limiter = plat, settings, metrics, limiter

    @app.exception_handler(UnknownTenant)
    async def _unknown(request: Request, exc: UnknownTenant):
        return JSONResponse({"detail": "unknown tenant"}, status_code=404)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        t0 = time.perf_counter()
        request.state.tenant = None
        response = await call_next(request)
        dt = time.perf_counter() - t0
        tenant = request.state.tenant
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        if tenant:
            metrics.observe(tenant, path, response.status_code, dt)
        if path not in ("/healthz", "/metrics") and not path.startswith("/static"):
            log_event(
                "request",
                tenant_id=tenant,
                method=request.method,
                route=path,
                status=response.status_code,
                ms=round(dt * 1000, 2),
            )
        return response

    def tenant_ctx(
        request: Request,
        authorization: str | None = Header(None),
        x_tenant_id: str | None = Header(None),
        host: str | None = Header(None),
    ):
        try:
            tid, claims = resolve(authorization, x_tenant_id, host, settings.secret, settings.base_domain)
        except AuthError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        t = plat.registry.get(tid)
        if t is None or t["status"] == "deleted":
            raise HTTPException(404, "unknown tenant")
        request.state.tenant = tid
        cost_ = REPORT_COST if request.url.path in HEAVY_ROUTES else 1
        cm = limiter.admit(tid, t["tier"], cost_)
        try:
            cm.__enter__()
        except RateLimited as exc:
            metrics.rate_limited(tid, exc.reason)
            raise HTTPException(429, exc.reason, headers={"Retry-After": str(max(1, round(exc.retry_after)))}) from exc
        try:
            yield Ctx(tid, claims.get("sub", ""), claims.get("role", "member"), plat)
        finally:
            cm.__exit__(None, None, None)

    TenantCtx = Annotated[Ctx, Depends(tenant_ctx)]

    def admin(x_admin_key: str | None = Header(None)):
        if not x_admin_key or not hmac.compare_digest(x_admin_key, settings.admin_key):
            raise HTTPException(401, "admin key required")

    # tenant API -------------------------------------------------------------
    @app.get("/api/me")
    def me(c: TenantCtx):
        t = plat.tenant(c.tenant_id)
        return {"tenant_id": c.tenant_id, "user": c.user, "role": c.role, "pattern": t["pattern"], "tier": t["tier"]}

    @app.get("/api/users")
    def users(c: TenantCtx):
        return c.repo.list_users()

    @app.get("/api/projects")
    def projects(c: TenantCtx):
        return c.repo.list_projects()

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectIn, c: TenantCtx):
        return c.repo.create_project(body.name, body.description, c.user or None)

    @app.get("/api/projects/{pid}")
    def get_project(pid: str, c: TenantCtx):
        return _must(c.repo.get_project(pid))

    @app.patch("/api/projects/{pid}")
    def patch_project(pid: str, body: ProjectPatch, c: TenantCtx):
        return _must(c.repo.update_project(pid, **body.model_dump(exclude_none=True)))

    @app.delete("/api/projects/{pid}", status_code=204)
    def delete_project(pid: str, c: TenantCtx):
        if not c.repo.delete_project(pid):
            raise HTTPException(404, NOT_FOUND)

    @app.get("/api/projects/{pid}/tasks")
    def project_tasks(pid: str, c: TenantCtx, status: str | None = None):
        repo = c.repo
        _must(repo.get_project(pid))
        return repo.list_tasks(project_id=pid, status=status)

    @app.post("/api/projects/{pid}/tasks", status_code=201)
    def create_task(pid: str, body: TaskIn, c: TenantCtx):
        return _must(c.repo.create_task(pid, body.title, body.body, body.assignee_id, priority=body.priority))

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, c: TenantCtx):
        return _must(c.repo.get_task(task_id))

    @app.patch("/api/tasks/{task_id}")
    def patch_task(task_id: str, body: TaskPatch, c: TenantCtx):
        return _must(c.repo.update_task(task_id, **body.model_dump(exclude_none=True)))

    @app.get("/api/tasks/{task_id}/comments")
    def comments(task_id: str, c: TenantCtx):
        repo = c.repo
        _must(repo.get_task(task_id))
        return repo.list_comments(task_id)

    @app.post("/api/tasks/{task_id}/comments", status_code=201)
    def add_comment(task_id: str, body: CommentIn, c: TenantCtx):
        return _must(c.repo.add_comment(task_id, body.body, c.user or None))

    @app.get("/api/tasks/{task_id}/attachments")
    def attachments(task_id: str, c: TenantCtx):
        repo = c.repo
        _must(repo.get_task(task_id))
        return repo.list_attachments(task_id)

    @app.post("/api/tasks/{task_id}/attachments", status_code=201)
    def add_attachment(task_id: str, body: AttachmentIn, c: TenantCtx):
        return _must(c.repo.add_attachment(task_id, body.filename, body.content_type, body.size_bytes, body.sha256))

    @app.get("/api/search")
    def search(q: str, c: TenantCtx):
        return c.repo.search(q[:100])

    @app.get("/api/export")
    def export(c: TenantCtx):
        return c.repo.export()

    @app.get("/api/reports/summary")
    def report(c: TenantCtx):
        return c.repo.report()

    @app.post("/api/jobs/report", status_code=202)
    def report_job(c: TenantCtx):
        tid = c.tenant_id  # captured now: the worker never reads tenant from ambient state
        return plat.jobs.submit(tid, "report", lambda: plat.repo(tid).report())

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, c: TenantCtx):
        return _must(plat.jobs.get(c.tenant_id, job_id))

    # admin and ops ------------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        plat.registry.q("SELECT 1")
        return {"status": "ok", "tenants": len(plat.registry.all()), "schema_latest": LATEST}

    @app.get("/metrics", response_class=PlainTextResponse)
    def prom():
        gauges = {}
        for p in PATTERNS:
            gauges[f'tenancylab_tenants{{pattern="{p}"}}'] = len(plat.registry.all(p))
            gauges[f'tenancylab_pool_wait_seconds{{pattern="{p}"}}'] = round(plat.backends[p].pool_wait_seconds(), 6)
        return metrics.render(gauges)

    @app.get("/admin/tenants")
    def admin_tenants():
        rows = plat.tenants()
        stats = metrics.per_tenant()
        return [{k: v for k, v in {**t, "traffic": stats.get(t["id"], {})}.items()} for t in rows]

    @app.post("/admin/tenants", status_code=201, dependencies=[Depends(admin)])
    def admin_onboard(body: TenantIn):
        try:
            return plat.onboard(body.id, body.name, body.pattern, body.tier, admin_email=body.admin_email)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/admin/tenants/{tid}", dependencies=[Depends(admin)])
    def admin_offboard(tid: str):
        try:
            report = plat.offboard(tid)
        except UnknownTenant as exc:
            raise HTTPException(404, "unknown tenant") from exc
        limiter.reset(tid)
        return report

    @app.get("/admin/tenants/{tid}/export", dependencies=[Depends(admin)])
    def admin_export(tid: str):
        try:
            return plat.export(tid)
        except UnknownTenant as exc:
            raise HTTPException(404, "unknown tenant") from exc

    @app.post("/admin/tenants/{tid}/backup", dependencies=[Depends(admin)])
    def admin_backup(tid: str):
        try:
            r = plat.backup(tid)
        except UnknownTenant as exc:
            raise HTTPException(404, "unknown tenant") from exc
        return {**r, "backup": Path(r["path"]).name}

    @app.post("/admin/tenants/{tid}/restore", dependencies=[Depends(admin)])
    def admin_restore(tid: str, body: RestoreIn):
        path = plat.dir / "backups" / body.backup
        if not body.backup.startswith(tid + "-") or not path.exists():
            raise HTTPException(404, "backup not found")
        return plat.restore(tid, path)

    @app.post("/admin/tenants/{tid}/move", dependencies=[Depends(admin)])
    def admin_move(tid: str, body: MoveIn):
        try:
            return plat.move(tid, body.to)
        except UnknownTenant as exc:
            raise HTTPException(404, "unknown tenant") from exc
        except MoveFailed as exc:
            return JSONResponse(exc.report, status_code=409)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/admin/tenants/{tid}/token", dependencies=[Depends(admin)])
    def admin_token(tid: str, sub: str = "usr_demo", role: str = "member"):
        plat.tenant(tid)
        return {"token": issue_token(settings.secret, tid, sub, role)}

    @app.get("/admin/migrations")
    def admin_migrations():
        return plat.migrations()

    @app.post("/admin/migrations", dependencies=[Depends(admin)])
    def admin_migrate(body: MigrationIn):
        run_id = plat.start_migration(body.pattern, body.target)
        return plat.run_migration(run_id, set(body.fail_units))

    @app.post("/admin/migrations/{run_id}/resume", dependencies=[Depends(admin)])
    def admin_resume(run_id: int):
        if not plat.registry.q("SELECT id FROM migration_runs WHERE id = ?", (run_id,)):
            raise HTTPException(404, "unknown run")
        return plat.run_migration(run_id)

    def _result(name: str):
        path = Path(settings.results_dir) / name
        if not path.exists():
            raise HTTPException(404, f"{name} not generated yet: run make bench")
        return json.loads(path.read_text())

    @app.get("/admin/noisy-neighbor")
    def admin_noisy():
        return _result("noisy_neighbor.json")

    @app.get("/admin/isolation")
    def admin_isolation():
        return _result("isolation.json")

    @app.get("/admin/migration-effort")
    def admin_effort():
        return _result("migration_effort.json")

    @app.get("/admin/cost")
    def admin_cost():
        return cost.model()

    @app.get("/admin/events")
    def admin_events(limit: int = 30):
        return plat.registry.events(min(limit, 200))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
