"""The admin listener's app (E17, E18): the local UI and the admin API, behind AdminGuard.

The app is FastAPI (D16) with its OpenAPI document and /docs and /redoc turned off: the contract
is packages/api-contracts, and nothing here describes the admin surface to whoever asks. Bodies
are read as they come; no request model stands beside validate() (D17).

The local UI is server-rendered HTML: Captures and their Attachments, each original as a download,
the import form a file is dropped on, and the jobs with retry and cancel (MVP-5); backups to a drive
with the overdue warning (U3); and the purge of one Blob, after a page that shows what goes, asks
about the fork Notes and says the backup generations still hold it (U2). An original is always sent
as an attachment, as octet-stream, nosniff and in a sandbox, so nothing a file holds runs in this
origin. The admin API under /api/ is what the CLI uses while the Hub runs: login links, imports of
local paths, backups, retry, cancel, status and stop (E11).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import backup, jobs, purge
from .admin_auth import HELLO_PATH, LISTENER_SCOPE_KEY, SESSION_COOKIE, SESSION_LIFETIME, AdminAuth, AdminGuard, session_cookie
from .admin_page import page
from .backup import BackupRefused, BackupSettings
from .core import Core
from .job_runner import JobRunner
from .jobs import Job, JobState, NoSuchJob, NotRetryable
from .listing import latest_captures
from .local_import import UnimportableFile, import_files
from .purge import PurgePlan, PurgeRefused
from .redaction import Redactor
from .staged_form import FormRefused, stage_form

PAGE_ROWS = 200
CLOUD_POLICIES = ("blocked", "local_only", "cloud_allowed")
RETRYABLE = frozenset({JobState.FAILED, JobState.BLOCKED})
CANCELLABLE = frozenset({JobState.PENDING, JobState.RETRY_WAIT, JobState.RUNNING})
SHA256 = re.compile(r"[0-9a-f]{64}")
BACKUP_FAILURES = (BackupRefused, OSError, sqlite3.Error)
NO_TELEMETRY = {"tracing": False, "metrics": False, "logs": False, "operation_spans": False, "auto_configure": False}
"""Request paths carry login codes; nothing about a request leaves the Hub but its redacted log."""

router = APIRouter()

STYLESHEET = """\
body { font: 15px/1.5 system-ui, sans-serif; margin: 0; color: #1d1d1f; background: #fafafa; }
nav { display: flex; gap: 1rem; align-items: center; padding: .75rem 1rem; background: #fff; border-bottom: 1px solid #ddd; }
nav form { margin-left: auto; }
main { max-width: 64rem; margin: 0 auto; padding: 1rem; }
table { border-collapse: collapse; width: 100%; background: #fff; }
th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid #eee; vertical-align: top; overflow-wrap: anywhere; }
code { font-size: .85em; }
.notice { padding: .5rem .75rem; background: #eef6ee; border: 1px solid #cde5cd; }
.warning { padding: .5rem .75rem; background: #fff4e5; border: 1px solid #f0c890; }
input[name=target] { width: 100%; box-sizing: border-box; }
.drop { display: block; width: 100%; box-sizing: border-box; padding: 3rem 1rem; border: 2px dashed #999; background: #fff; }
.drop:hover { border-color: #333; }
td form { display: inline; }
"""


@dataclass(frozen=True)
class Admin:
    """What the admin listener works with; the host makes it."""

    core: Core
    runner: JobRunner
    auth: AdminAuth
    redactor: Redactor
    status: Callable[[], Mapping[str, object]] = field(default=dict)
    """The host's report on itself, shown by /api/status."""
    stop: Callable[[], None] = field(default=lambda: None)
    """Ask the host to stop, as a signal would."""
    backup_settings: BackupSettings = BackupSettings()
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


def _admin(request: Request) -> Admin:
    return request.app.state.admin


@router.get("/static/admin.css")
async def stylesheet() -> Response:
    return Response(STYLESHEET, media_type="text/css")


def _period(span: timedelta) -> str:
    return f"{span.days} days" if span == timedelta(days=span.days) else str(span)


async def _backup_warning(admin: Admin) -> str:
    """The overdue warning (U3), or nothing while the last backup is recent enough."""
    last = await admin.core.db.read(backup.last_backup)
    if not backup.is_overdue(last, now=admin.clock(), warn_after=admin.backup_settings.warn_after):
        return ""
    said = "No backup yet." if last is None else f"No backup for more than {_period(admin.backup_settings.warn_after)}; the last one finished {escape(last.finished_at)}."
    return f'<p class="warning">{said} <a href="/backup">Back up to a drive</a>.</p>'


@router.get("/")
async def captures_page(request: Request) -> Response:
    admin = _admin(request)
    captures = await admin.core.db.read(lambda snap: latest_captures(snap, PAGE_ROWS))
    imported = request.query_params.get("imported")
    rows = []
    for capture in captures:
        files = "<br>".join(
            f'<a href="/attachments/{escape(attachment.id)}/original">{escape(attachment.original_filename or attachment.blob_sha256[:12])}</a>'
            f' <small>{escape(attachment.cloud_policy)}</small> <small><a href="/blobs/{escape(attachment.blob_sha256)}/purge">purge</a></small>'
            for attachment in capture.attachments
        )
        rows.append(f"<tr><td>{escape(capture.created_at)}</td><td>{escape(capture.origin_type)}</td><td>{files or '-'}</td></tr>")
    notice = f'<p class="notice">Imported as Capture <code>{escape(imported)}</code>.</p>' if imported else ""
    table = "<table><tr><th>Taken</th><th>From</th><th>Files</th></tr>" + "".join(rows) + "</table>" if rows else "<p>No Captures yet.</p>"
    return page("Captures", await _backup_warning(admin) + notice + table)


@router.get("/attachments/{attachment_id}/original")
async def original(request: Request, attachment_id: str) -> Response:
    core = _admin(request).core
    row = await core.db.read(lambda snap: snap.execute("SELECT data FROM attachments WHERE id = ?", (attachment_id,)).fetchone())
    if row is None:
        return page("Not found", "<p>No such Attachment.</p>", status=404)
    attachment = json.loads(row[0])
    path = core.blobs.path_of(attachment["blob_sha256"])
    if not path.is_file():
        return page("Missing", "<p>The original is missing from the Blob store.</p>", status=404)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=attachment.get("original_filename") or attachment["blob_sha256"],
        headers={"x-content-type-options": "nosniff", "content-security-policy": "sandbox"},
    )


@router.get("/import")
async def import_page() -> Response:
    options = '<option value="">the course or global default</option>' + "".join(f"<option>{policy}</option>" for policy in CLOUD_POLICIES)
    return page(
        "Import",
        '<form method="post" action="/import" enctype="multipart/form-data">'
        '<p><input class="drop" type="file" name="files" multiple required aria-label="Drop files here or choose them"></p>'
        f'<p><label>cloud_policy <select name="cloud_policy">{options}</select></label></p>'
        "<p><button>Import as one Capture</button></p></form>",
    )


def _refused(title: str, reason: str) -> Response:
    return page(title, f"<p>{escape(reason)}</p>", status=400)


@router.post("/import")
async def import_form(request: Request) -> Response:
    admin = _admin(request)
    try:
        form = await stage_form(request.headers.get("content-type", ""), request.stream(), admin.core.layout.staging)
    except FormRefused as error:
        return _refused("Import refused", str(error))
    try:
        policy = form.fields.get("cloud_policy") or None
        if not form.files:
            return _refused("Import refused", "choose or drop at least one file")
        if policy is not None and policy not in CLOUD_POLICIES:
            return _refused("Import refused", f"cloud_policy is one of {', '.join(CLOUD_POLICIES)}")
        imported = await import_files(admin.core, [staged.path for staged in form.files], names=[staged.name for staged in form.files], cloud_policy=policy)
    except UnimportableFile as error:
        return _refused("Import refused", str(error))
    finally:
        form.discard()
    admin.runner.wake()
    if imported.result["result"] != "applied":
        return _refused("Import refused", f"the Hub answered {imported.result['result']}: {imported.result.get('reason', '')}")
    return RedirectResponse(f"/?imported={imported.capture_id}", status_code=303)


def _job_row(job: Job, redactor: Redactor) -> str:
    actions = []
    if job.state in RETRYABLE:
        actions.append(f'<form method="post" action="/jobs/{escape(job.id)}/retry"><button>Retry</button></form>')
    if job.state in CANCELLABLE:
        actions.append(f'<form method="post" action="/jobs/{escape(job.id)}/cancel"><button>Cancel</button></form>')
    error = redactor.redact(job.error or "")[:300]
    return (
        f"<tr><td>{escape(job.job_type)}</td><td>{escape(job.state)}</td><td>{job.attempts}</td><td>{escape(job.subject_ref)}</td>"
        f"<td>{escape(job.outcome_code or '')}</td><td>{escape(error)}</td><td>{escape(job.updated_at)}</td><td>{''.join(actions)}</td></tr>"
    )


@router.get("/jobs")
async def jobs_page(request: Request) -> Response:
    admin = _admin(request)
    latest = await admin.core.db.read(lambda snap: jobs.latest(snap, PAGE_ROWS))
    if not latest:
        return page("Jobs", "<p>No jobs yet.</p>")
    head = "<tr><th>Type</th><th>State</th><th>Attempts</th><th>Subject</th><th>Outcome</th><th>Error</th><th>Changed</th><th></th></tr>"
    return page("Jobs", "<table>" + head + "".join(_job_row(job, admin.redactor) for job in latest) + "</table>")


async def _retry(admin: Admin, job_id: str) -> Job:
    job = await admin.core.db.write(lambda tx: jobs.retry(tx, job_id, now=datetime.now(UTC)))
    admin.runner.wake()
    return job


async def _cancel(admin: Admin, job_id: str) -> Job:
    return await admin.runner.cancel(job_id)


def _job_change(change: Callable[[Admin, str], object], *, api: bool) -> Callable[[Request, str], object]:
    async def endpoint(request: Request, job_id: str) -> Response:
        try:
            job = await change(_admin(request), job_id)
        except NoSuchJob:
            return JSONResponse({"error": "no such job"}, status_code=404) if api else page("Not found", "<p>No such job.</p>", status=404)
        except NotRetryable as error:
            return JSONResponse({"error": str(error)}, status_code=409) if api else page("Not retried", f"<p>{escape(str(error))}</p>", status=409)
        return JSONResponse({"id": job.id, "state": job.state}) if api else RedirectResponse("/jobs", status_code=303)

    return endpoint


@router.get("/backup")
async def backup_page(request: Request) -> Response:
    admin = _admin(request)
    last = await admin.core.db.read(backup.last_backup)
    done = (
        f"<p>The last backup, generation <code>{escape(last.generation)}</code>, finished {escape(last.finished_at)} in <code>{escape(last.target)}</code>.</p>"
        if last is not None
        else ""
    )
    return page(
        "Backup",
        await _backup_warning(admin) + done
        + '<form method="post" action="/backup"><p><label>Folder on the backup drive '
        f'<input name="target" value="{escape(last.target) if last else ""}" required></label></p>'
        "<p><button>Back up now</button></p></form>"
        f"<p>The drive keeps the newest {admin.backup_settings.keep} generations. Backups are not encrypted: keep the drive safe.</p>",
    )


def _generation_report(made: backup.Generation) -> str:
    missing = (
        f'<p class="warning">{len(made.missing)} Blobs were missing from the Hub and are not in it; run <code>campuslife-hub fsck</code>.</p>'
        if made.missing
        else ""
    )
    pruned = f"<p>Removed the older generations {escape(', '.join(made.pruned))}.</p>" if made.pruned else ""
    return (
        f"<p>Generation <code>{escape(made.name)}</code> in <code>{escape(str(made.target))}</code>: {made.blobs} Blobs, {made.copied} copied.</p>"
        + missing + pruned
    )


@router.post("/backup")
async def backup_form(request: Request) -> Response:
    admin = _admin(request)
    target = str((await request.form()).get("target") or "").strip()
    if not target:
        return _refused("Backup refused", "name the folder on the backup drive")
    try:
        made = await backup.back_up(admin.core, Path(target), keep=admin.backup_settings.keep)
    except BACKUP_FAILURES as error:
        return _refused("Backup failed", str(error))
    return page("Backed up", _generation_report(made))


def _purge_plan_page(plan: PurgePlan, keep: int) -> Response:
    target = plan.target
    files = escape(", ".join(plan.filenames)) or "no file names"
    gone = (
        f"<li>{len(target.attachments)} Attachments: {files}</li>"
        f"<li>{len(target.artifacts)} Artifacts made from it, such as transcripts, summaries and renditions</li>"
        f"<li>{len(target.suggestions)} Suggestions</li><li>{len(target.relations)} Relations</li>"
    )
    notes = ""
    if plan.fork_notes:
        boxes = "".join(
            f'<li><label><input type="checkbox" name="delete_note" value="{escape(note.id)}"> {escape(note.title)}'
            f"{' (in the trash)' if note.deleted else ''}</label></li>"
            for note in plan.fork_notes
        )
        notes = (
            "<fieldset><legend>Delete these Notes too?</legend><p>They were forked from what goes. "
            f"Those not chosen stay, marked as having lost their evidence.</p><ul>{boxes}</ul></fieldset>"
        )
    return page(
        "Purge",
        f"<p>Purging removes this file at once, without the trash, with everything made from it:</p><ul>{gone}</ul>"
        f'<form method="post" action="/blobs/{escape(plan.blob)}/purge">{notes}'
        '<p class="notice">Backup generations on the backup drive still hold it and what was made from it; they leave the drive '
        f"as newer backups replace them (the newest {keep} are kept).</p><p><button>Purge now</button></p></form>",
    )


async def _plan(admin: Admin, blob: str) -> PurgePlan | None:
    if not SHA256.fullmatch(blob):
        return None
    try:
        return await admin.core.db.read(lambda snap: purge.plan(snap, blob))
    except PurgeRefused:
        return None


@router.get("/blobs/{blob}/purge")
async def purge_page(request: Request, blob: str) -> Response:
    admin = _admin(request)
    plan = await _plan(admin, blob)
    if plan is None:
        return page("Not found", "<p>The Hub holds nothing of that Blob.</p>", status=404)
    return _purge_plan_page(plan, admin.backup_settings.keep)


@router.post("/blobs/{blob}/purge")
async def purge_form(request: Request, blob: str) -> Response:
    admin = _admin(request)
    if await _plan(admin, blob) is None:
        return page("Not found", "<p>The Hub holds nothing of that Blob.</p>", status=404)
    chosen = [str(note) for note in (await request.form()).getlist("delete_note")]
    try:
        purged = await purge.purge_blob(admin.core.db, admin.core.blobs, blob, delete_notes=chosen, hooks=admin.core.purge_hooks)
    except PurgeRefused as error:
        return _refused("Purge refused", str(error))
    for job_id in purged.cancelled:
        await admin.runner.cancel(job_id)
    target = purged.target
    left = (
        '<p class="warning">Part of the clean-up is left for the next maintenance: '
        + escape("; ".join([*(f"hook {name} failed" for name in purged.failed_hooks), *(f"{kept.path}: {kept.reason}" for kept in purged.kept)]))
        + "</p>"
        if not purged.finished
        else ""
    )
    return page(
        "Purged",
        f"<p>Removed {len(target.attachments)} Attachments, {len(target.artifacts)} Artifacts, {len(target.suggestions)} Suggestions, "
        f"{len(target.relations)} Relations and {len(target.items)} Notes.</p>"
        + (f"<p>{len(purged.marked)} Notes stay, marked as having lost their evidence.</p>" if purged.marked else "")
        + left,
    )


@router.get("/login")
async def login(request: Request) -> Response:
    code = request.query_params.get("code")
    session = _admin(request).auth.start_session(code) if code else None
    if session is None:
        return page(
            "Log in",
            "<p>This login link is used or expired. Run <code>campuslife-hub open</code> for a new one.</p>",
            status=401,
            signed_in=False,
        )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, session, max_age=int(SESSION_LIFETIME.total_seconds()), path="/", httponly=True, samesite="strict")
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    session = session_cookie(request.headers)
    if session is not None:
        _admin(request).auth.end_session(session)
    response = page("Logged out", "<p>Run <code>campuslife-hub open</code> to log in again.</p>", signed_in=False)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


@router.post(HELLO_PATH)
async def api_hello(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        body = None
    fields = body if isinstance(body, dict) else {}
    answer = _admin(request).auth.hello(request.scope[LISTENER_SCOPE_KEY], fields.get("nonce"), fields.get("mac"))
    if answer is None:
        return JSONResponse({"error": "a hello is {nonce: 64 hex digits, mac: their HMAC under the admin token}"}, status_code=401)
    hub_nonce, proof = answer
    return JSONResponse({"nonce": hub_nonce, "proof": proof})


@router.post("/api/login")
async def api_login(request: Request) -> Response:
    code = _admin(request).auth.issue_login_code()
    return JSONResponse({"url": f"http://{request.scope[LISTENER_SCOPE_KEY]}/login?code={code}"})


def _strings(value: object) -> list[str] | None:
    return value if isinstance(value, list) and all(isinstance(item, str) for item in value) else None


@router.post("/api/import")
async def api_import(request: Request) -> Response:
    admin = _admin(request)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    paths, links, policy = _strings(body.get("paths")), _strings(body.get("links", [])), body.get("cloud_policy")
    if not paths or links is None or policy not in (None, *CLOUD_POLICIES):
        return JSONResponse({"error": "the body is {paths: [str, ...], links?: [str, ...], cloud_policy?: str}"}, status_code=400)
    try:
        imported = await import_files(admin.core, [Path(path) for path in paths], links=links, cloud_policy=policy)
    except (UnimportableFile, OSError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    admin.runner.wake()
    return JSONResponse({"capture_id": imported.capture_id, "result": imported.result})


@router.post("/api/backup")
async def api_backup(request: Request) -> Response:
    admin = _admin(request)
    try:
        body = await request.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    target, keep = body.get("target"), body.get("keep", admin.backup_settings.keep)
    if not isinstance(target, str) or not target.strip() or type(keep) is not int or keep < 1:
        return JSONResponse({"error": "the body is {target: a folder on the backup drive, keep?: generations to keep, at least 1}"}, status_code=400)
    try:
        made = await backup.back_up(admin.core, Path(target), keep=keep)
    except BACKUP_FAILURES as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return JSONResponse(
        {"generation": made.name, "target": str(made.target), "finished_at": made.finished_at, "blobs": made.blobs, "copied": made.copied,
         "missing": list(made.missing), "pruned": list(made.pruned)}
    )


@router.get("/api/status")
async def api_status(request: Request) -> Response:
    admin = _admin(request)
    counts = await admin.core.db.read(jobs.counts)
    last = await admin.core.db.read(backup.last_backup)
    settings = admin.backup_settings
    return JSONResponse(
        {
            "hub_id": admin.core.identity.hub_id,
            "data_root": str(admin.core.layout.root),
            "jobs": {state.value: count for state, count in counts.items()},
            "backup": {
                "last": None if last is None else asdict(last),
                "overdue": backup.is_overdue(last, now=admin.clock(), warn_after=settings.warn_after),
                "warn_after_days": settings.warn_after.days,
                "keep": settings.keep,
            },
            **admin.status(),
        }
    )


@router.post("/api/stop")
async def api_stop(request: Request) -> Response:
    _admin(request).stop()
    return JSONResponse({"stopping": True})


for action, change in (("retry", _retry), ("cancel", _cancel)):
    router.add_api_route(f"/jobs/{{job_id}}/{action}", _job_change(change, api=False), methods=["POST"])
    router.add_api_route(f"/api/jobs/{{job_id}}/{action}", _job_change(change, api=True), methods=["POST"])


def create_app(admin: Admin, *, port: int, routers: Sequence[APIRouter] = ()) -> AdminGuard:
    """The admin app for the listener on 127.0.0.1:port; routers are added behind the same guard (#19)."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, telemetry=NO_TELEMETRY)
    for added in (router, *routers):
        app.include_router(added)
    app.state.admin = admin
    return AdminGuard(app, admin.auth, port)
