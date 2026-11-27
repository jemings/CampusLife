"""The local UI's page frame, shared by the admin routes and the routes added beside them (#19's devices)."""

from __future__ import annotations

from html import escape

from fastapi.responses import HTMLResponse


def page(title: str, body: str, *, status: int = 200, signed_in: bool = True) -> HTMLResponse:
    """A local UI page: body is HTML the caller has escaped; title is escaped here."""
    nav = (
        '<nav><a href="/">Captures</a><a href="/import">Import</a><a href="/jobs">Jobs</a><a href="/backup">Backup</a>'
        '<form method="post" action="/logout"><button>Log out</button></form></nav>'
        if signed_in
        else ""
    )
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(title)} · CampusLife Hub</title><link rel="stylesheet" href="/static/admin.css"></head>'
        f"<body>{nav}<main><h1>{escape(title)}</h1>{body}</main></body></html>",
        status_code=status,
    )
