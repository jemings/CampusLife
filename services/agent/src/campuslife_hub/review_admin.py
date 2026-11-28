"""The Inbox and Suggestion review on the admin listener (#9): the local UI's pages, beside the admin routes.

- GET /inbox: the open Suggestions with their evidence, and the unclassified Captures (#9 decision 1), each part alone
  with ?show=suggestions or ?show=captures. One form holds every Suggestion: a box to choose it, Accept and Dismiss for
  it alone, and the two for every chosen one.
- POST /inbox/decide: those decisions, each its own Mutation, so one the Hub refuses does not hold back the others; a
  page then gives each one's answer.
- GET /suggestions/{id} and POST /suggestions/{id}/accept: one Suggestion, accepted with edits (#9 decision 3).
- GET /items/{id}: an Item with its evidence and Tags; POST /items/{id}/tags adds a Tag, a new one or one of the same
  name, and POST /items/{id}/tags/{tag_id}/remove takes it off.
- POST /captures/{id}/item: makes an Item of an unclassified Capture, with an evidence Relation to it.

Every write is a Mutation through the applier, sent as the Hub's own Device. Each mutation_id, and the id of an Item or
Tag a form makes, is drawn into the page when it is rendered, so a form sent twice is a duplicate the second time.
What a Suggestion or a Capture carries came from a model or a file and is escaped like anything else shown.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING

from campuslife_contracts import contract_root
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from . import inbox, mutations, store
from .admin_page import page
from .db import Snapshot
from .store import Entity, EntityRef
from .suggestions import KIND_FIELDS

if TYPE_CHECKING:
    from .admin import Admin

router = APIRouter()

INBOX_ROWS = 200
"""Of each part of the Inbox, the most one page shows."""
SHOWN = ("all", "suggestions", "captures")
DECISIONS = ("accept", "dismiss")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
COLOR = re.compile(r"#[0-9a-f]{6}")
ITEM_COLUMNS = ("title", "body", "starts_at", "due_at", "all_day")
"""The columns an edit may give an Item it creates, besides its fields; kind is not one (#9 decision 4)."""
TEXT_AS_TEXT = "Text that reads as JSON (a number, true, false, null, an object or a list) is written as that value."


def _required_fields(kind: str) -> list[str]:
    schema = json.loads((contract_root() / "schemas" / "item" / "fields" / f"{kind}.json").read_text(encoding="utf-8"))
    return schema.get("required", [])


MADE_FROM_A_CAPTURE = tuple(sorted(kind for kind in KIND_FIELDS if not _required_fields(kind)))
"""The kinds an Item made of a Capture may take: those whose fields need nothing this form does not ask."""


def _admin(request: Request) -> Admin:
    return request.app.state.admin


def _new_id() -> str:
    return str(uuid.uuid4())


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{escape(name)}" value="{escape(value)}">'


def normalized(text: str) -> str:
    """What a form sends, as the Hub keeps text: NFC with LF line ends."""
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _ms(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def locator_text(locator: dict) -> str:
    """Where a Locator points, as a person reads it (#9 decision 10). Not escaped."""
    match locator.get("kind"):
        case "pdf_page":
            return f"p.{locator['page']}" + (", part of the page" if "box" in locator else "")
        case "slide":
            return f"slide {locator['slide']}" + (f", shape {locator['shape']}" if "shape" in locator else "")
        case "doc_path":
            paragraph = f"¶{locator['block_index'] + 1}"
            return f"§ {' › '.join(locator['heading_path'])} {paragraph}" if locator["heading_path"] else paragraph
        case "sheet_range":
            return f"{locator['sheet']}!{locator['a1']}" if "sheet" in locator else locator["a1"]
        case "media_time":
            start, end = _ms(locator["start_ms"]), _ms(locator["end_ms"])
            return start if start == end else f"{start}–{end}"
        case "image_region":
            return "part of the image"
        case "web_snapshot":
            return locator["url"] + (f"#{locator['anchor']}" if "anchor" in locator else "")
        case "document":
            return "the whole file"
        case kind:
            return f"a place of kind {kind} this Hub cannot show"


def _anchor(locator: dict) -> str | None:
    if "blob" in locator:
        return f"blob:{locator['blob']}"
    if "capture" in locator:
        return f"capture:{locator['capture']}"
    return None


def _originals(snap: Snapshot, anchor: str | None) -> str:
    """Links to the files an anchor is: the Attachments of a Blob or of a Capture."""
    kind, _, identifier = (anchor or "").partition(":")
    if kind not in ("blob", "capture"):
        return ""
    column = "blob_sha256" if kind == "blob" else "capture_id"
    attachments = [json.loads(data) for (data,) in snap.execute(f"SELECT data FROM attachments WHERE {column} = ? ORDER BY id", (identifier,))]
    if not attachments:
        return " <small>(the Hub no longer holds the file)</small>"
    return " " + ", ".join(
        f'<a href="/attachments/{escape(attachment["id"])}/original">{escape(attachment.get("original_filename") or attachment["blob_sha256"][:12])}</a>'
        for attachment in attachments
    )


def _evidence(snap: Snapshot, locators: Sequence[dict]) -> str:
    if not locators:
        return "<p><small>No evidence.</small></p>"
    lines = (f"<li>{escape(locator_text(locator))}{_originals(snap, _anchor(locator))}</li>" for locator in locators)
    return f'<ul class="evidence">{"".join(lines)}</ul>'


def _item_title(snap: Snapshot, item_id: str) -> str:
    item = store.get(snap, EntityRef(Entity.ITEM, item_id))
    return item["title"] if item else item_id


def label(snap: Snapshot, suggestion: dict) -> str:
    """What a Suggestion proposes, in a line. Not escaped."""
    proposed = suggestion["proposed"]
    match suggestion["action"]:
        case "create_item":
            return proposed["title"]
        case "patch_item":
            return f"Change {', '.join(proposed['set'])} of {_item_title(snap, proposed['id'])}"
        case "add_relation":
            return f"{proposed['from']} {proposed['type']} {proposed['to']}"
        case action:
            return f"{action} (this Hub can only dismiss it)"


def _conflicts(snap: Snapshot, suggestion: dict) -> str:
    """#9 decision 9: the Hub does not pick between conflicting Suggestions, it shows them."""
    notes = []
    for other_id in suggestion.get("conflicts_with", []):
        other = store.get(snap, EntityRef(Entity.SUGGESTION, other_id))
        if other is None:
            continue
        said = " You accepted it." if other["state"] == "accepted" else ""
        notes.append(f'<a href="/suggestions/{escape(other_id)}">{escape(label(snap, other))}</a>{said}')
    return f'<p class="warning">Conflicts with {"; ".join(notes)}</p>' if notes else ""


def _suggestion_card(snap: Snapshot, suggestion: dict) -> str:
    sid = escape(suggestion["id"])
    accept = suggestion["action"] in ("create_item", "patch_item", "add_relation")
    buttons = (f'<button name="decide" value="{sid}:accept">Accept</button>' if accept else "") + (
        f'<button name="decide" value="{sid}:dismiss">Dismiss</button> <a href="/suggestions/{sid}">Edit and accept</a>'
    )
    kind = f" <small>{escape(suggestion['proposed'].get('kind', ''))}</small>" if suggestion["action"] == "create_item" else ""
    return (
        f'<section class="card"><label><input type="checkbox" name="chosen" value="{sid}"> '
        f'<a href="/suggestions/{sid}">{escape(label(snap, suggestion))}</a></label>{kind}'
        f"{_conflicts(snap, suggestion)}{_evidence(snap, suggestion['evidence'])}"
        + "".join(_hidden(f"mutation_id.{suggestion['id']}.{decision}", _new_id()) for decision in DECISIONS)
        + f"<p>{buttons}</p></section>"
    )


def _emptied(capture: dict, attachments: list[dict]) -> bool:
    """A Capture is committed with a file, a url or a note, so one with none of them had its files purged."""
    return not attachments and "url" not in capture and "user_note" not in capture


def _capture_card(snap: Snapshot, capture: dict) -> str:
    cid = capture["id"]
    attachments = [json.loads(data) for (data,) in snap.execute("SELECT data FROM attachments WHERE capture_id = ? ORDER BY id", (cid,))]
    files = ", ".join(
        f'<a href="/attachments/{escape(attachment["id"])}/original">{escape(attachment.get("original_filename") or attachment["blob_sha256"][:12])}</a>'
        for attachment in attachments
    )
    said = [f"<small>{escape(capture['created_at'])} · {escape(capture['origin']['type'])}</small>"]
    if files:
        said.append(files)
    if "url" in capture:
        said.append(f"<code>{escape(capture['url'])}</code>")
    if "user_note" in capture:
        said.append(f"<q>{escape(capture['user_note'])}</q>")
    if _emptied(capture, attachments):
        said.append(
            '<span class="warning">Its files were purged; only the record of taking it is left. It is back in the Inbox '
            "because what classified it rested on those files.</span>"
        )
    kinds = "".join(f"<option>{kind}</option>" for kind in MADE_FROM_A_CAPTURE)
    action = f"/captures/{escape(cid)}/item"
    form = (
        f'<form method="post" action="{action}">'
        + _hidden("item_id", _new_id()) + _hidden("item_mutation_id", _new_id()) + _hidden("relation_mutation_id", _new_id())
        + f'<label>Kind <select name="kind">{kinds}</select></label> <label>Title <input name="title" required></label> '
        "<button>Make an Item of it</button></form>"
    )
    return f'<section class="card" id="capture-{escape(cid)}"><p>{"<br>".join(said)}</p>{form}</section>'


@dataclass(frozen=True)
class InboxPage:
    box: inbox.Inbox
    suggestions: str
    captures: str


def _inbox_page(snap: Snapshot) -> InboxPage:
    box = inbox.compute(snap)
    return InboxPage(
        box,
        "".join(_suggestion_card(snap, suggestion) for suggestion in box.suggestions[:INBOX_ROWS]),
        "".join(_capture_card(snap, capture) for capture in box.captures[:INBOX_ROWS]),
    )


def _more(total: int) -> str:
    return f"<p><small>{total - INBOX_ROWS} more; they show as these are decided.</small></p>" if total > INBOX_ROWS else ""


@router.get("/inbox")
async def inbox_page(request: Request) -> Response:
    shown = request.query_params.get("show")
    shown = shown if shown in SHOWN else "all"
    drawn = await _admin(request).core.db.read(_inbox_page)
    filters = " · ".join(
        f"<strong>{name}</strong>" if name == shown else f'<a href="/inbox?show={name}">{name}</a>' for name in SHOWN
    )
    parts = [f"<p>{filters}</p>"]
    if shown != "captures":
        suggestions = (
            '<form method="post" action="/inbox/decide">' + drawn.suggestions
            + '<p><button name="decide" value="chosen:accept">Accept the chosen</button> '
            '<button name="decide" value="chosen:dismiss">Dismiss the chosen</button> '
            "<small>Each is decided on its own; the next page gives each one's answer.</small></p></form>"
            if drawn.suggestions
            else "<p>No Suggestion is waiting.</p>"
        )
        parts.append(f"<h2>Suggestions ({len(drawn.box.suggestions)})</h2>{suggestions}{_more(len(drawn.box.suggestions))}")
    if shown != "suggestions":
        captures = drawn.captures or "<p>Every Capture is classified.</p>"
        parts.append(
            f"<h2>Unclassified Captures ({len(drawn.box.captures)})</h2>"
            "<p><small>A Capture leaves the Inbox once a Relation reaches it, one of its files or their Blob: accepting a "
            "Suggestion with evidence in it, or making an Item of it. An Item in the trash keeps its Relations, so what it "
            f"rests on stays out of the Inbox.</small></p>{captures}{_more(len(drawn.box.captures))}"
        )
    return page("Inbox", "".join(parts))


def _refused(reason: str, status: int = 400) -> Response:
    return page("Refused", f"<p>{escape(reason)}</p>", status=status)


def _answer(result: dict) -> str:
    """A Mutation's result in words. Not escaped."""
    match result["result"]:
        case "applied":
            return "applied"
        case "conflict":
            return f"conflict: {', '.join(result['conflicted_fields'])} changed since; kept: {', '.join(result['applied_fields']) or 'nothing'}"
        case "rejected":
            return f"rejected: {result['reason']}"
        case "duplicate":
            return f"sent before; the first answer was {_answer(result['original'])}"
        case other:
            return other


def _outcome(result: dict) -> dict:
    return result["original"] if result["result"] == "duplicate" else result


def _where_it_went(snap: Snapshot, suggestion_id: str) -> str | None:
    """The Item an accepted Suggestion made or changed."""
    suggestion = store.get(snap, EntityRef(Entity.SUGGESTION, suggestion_id))
    if suggestion is None or suggestion["state"] != "accepted":
        return None
    if "created_item_id" in suggestion:
        return suggestion["created_item_id"]
    return suggestion["proposed"]["id"] if suggestion["action"] == "patch_item" else None


async def _apply(admin: Admin, batch: list[dict]) -> list[dict]:
    return await mutations.apply(admin.core.db, batch, admin.core.hub_device)


def _decisions(form: FormData) -> list[tuple[str, str, str]] | None:
    """(suggestion id, decision, mutation_id) for what the pressed button decides, or None for a form its page did not draw."""
    target, _, decision = str(form.get("decide") or "").rpartition(":")
    if decision not in DECISIONS:
        return None
    chosen = [str(each) for each in form.getlist("chosen")] if target == "chosen" else [target]
    decided = [(sid, decision, str(form.get(f"mutation_id.{sid}.{decision}") or "")) for sid in chosen]
    return decided if decided and all(UUID.fullmatch(sid) and UUID.fullmatch(mid) for sid, _, mid in decided) else None


@router.post("/inbox/decide")
async def decide_form(request: Request) -> Response:
    admin = _admin(request)
    decided = _decisions(await request.form())
    if decided is None:
        return _refused("Choose at least one Suggestion from the Inbox page, and send its form as the page drew it.")
    labels = await admin.core.db.read(
        lambda snap: {sid: label(snap, row) if (row := store.get(snap, EntityRef(Entity.SUGGESTION, sid))) else sid for sid, _, _ in decided}
    )
    results = await _apply(admin, [
        {"mutation_id": mid, "op": f"suggestion.{decision}", "suggestion_id": sid} for sid, decision, mid in decided
    ])
    went = await admin.core.db.read(lambda snap: {sid: _where_it_went(snap, sid) for sid, _, _ in decided})
    rows = []
    for (sid, decision, _), result in zip(decided, results, strict=True):
        link = f' <a href="/items/{escape(went[sid])}">Open the Item</a>' if decision == "accept" and went[sid] else ""
        rows.append(
            f'<tr class="result-{escape(result["result"])}"><td>{escape(labels[sid])}</td><td>{decision}</td>'
            f"<td>{escape(_answer(result))}{link}</td></tr>"
        )
    return page("Decided", "<table><tr><th>Suggestion</th><th>Decision</th><th>Answer</th></tr>" + "".join(rows) + "</table>"
                '<p><a href="/inbox">Back to the Inbox</a></p>')


def _shown(value: object) -> str:
    """A value as an edit field shows it: text as it is, anything else as JSON."""
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _surface(suggestion: dict) -> dict[str, object]:
    """Each key an accept's edits may name (#9 decision 3), with the value the Suggestion proposes for it."""
    proposed = suggestion["proposed"]
    match suggestion["action"]:
        case "create_item":
            columns = {key: proposed.get(key) for key in ITEM_COLUMNS}
            fields = {f"fields.{key}": proposed["fields"].get(key) for key in sorted(KIND_FIELDS.get(proposed["kind"], ()))}
            return columns | fields
        case "patch_item":
            return {key: change["to"] for key, change in proposed["set"].items()}
        case _:
            return {}


def edits_from(form: FormData, surface: dict[str, object]) -> dict[str, object]:
    """The keys whose field the user changed, with their new values. An emptied field is null; text in place of text
    stays text, and other text is JSON when it reads as JSON."""
    edits = {}
    for key, proposed in surface.items():
        sent = form.get(f"edit.{key}")
        if sent is None or normalized(str(sent)) == normalized(_shown(proposed)):
            continue
        text = normalized(str(sent))
        if text == "":
            edits[key] = None
        elif isinstance(proposed, str):
            edits[key] = text
        else:
            try:
                edits[key] = json.loads(text)
            except ValueError:
                edits[key] = text
    return edits


def _edit_field(key: str, value: object, before: object = None, *, patch: bool = False) -> str:
    name = f"edit.{key}"
    shown = escape(_shown(value))
    field = (
        f'<textarea name="{escape(name)}" rows="4">{shown}</textarea>' if key == "body" else f'<input name="{escape(name)}" value="{shown}">'
    )
    was = f" <small>now {escape(_shown(before) or 'empty')}</small>" if patch else ""
    return f"<p><label>{escape(key)} {field}</label>{was}</p>"


def _suggestion_page(snap: Snapshot, suggestion: dict) -> str:
    sid = suggestion["id"]
    parts = [f"<p>{escape(suggestion['action'])} · {escape(suggestion['state'])}</p>", _conflicts(snap, suggestion),
             "<h2>Evidence</h2>", _evidence(snap, suggestion["evidence"])]
    if suggestion["state"] != "open":
        went = _where_it_went(snap, sid)
        parts.append(f'<p><a href="/items/{escape(went)}">Open the Item</a></p>' if went else "")
        return "".join(parts)
    proposed, surface = suggestion["proposed"], _surface(suggestion)
    if suggestion["action"] == "create_item":
        fields = [f"<p>kind <strong>{escape(proposed['kind'])}</strong></p>"] + [_edit_field(key, value) for key, value in surface.items()]
    elif suggestion["action"] == "patch_item":
        fields = [_edit_field(key, value, proposed["set"][key].get("from"), patch=True) for key, value in surface.items()]
    else:
        fields = [f"<p>{escape(label(snap, suggestion))}</p>"]
    accept = (
        f'<form method="post" action="/suggestions/{escape(sid)}/accept">{_hidden("mutation_id", _new_id())}{"".join(fields)}'
        f"<p><small>An emptied field is left out. {TEXT_AS_TEXT}</small></p><p><button>Accept</button></p></form>"
        if surface or suggestion["action"] == "add_relation"
        else ""
    )
    dismiss = (
        f'<form method="post" action="/inbox/decide">{_hidden(f"mutation_id.{sid}.dismiss", _new_id())}'
        f'<p><button name="decide" value="{escape(sid)}:dismiss">Dismiss</button></p></form>'
    )
    return "".join(parts) + accept + dismiss


async def _suggestion(admin: Admin, suggestion_id: str) -> dict | None:
    return await admin.core.db.read(lambda snap: store.get(snap, EntityRef(Entity.SUGGESTION, suggestion_id)))


@router.get("/suggestions/{suggestion_id}")
async def suggestion_page(request: Request, suggestion_id: str) -> Response:
    admin = _admin(request)
    suggestion = await _suggestion(admin, suggestion_id)
    if suggestion is None:
        return page("Not found", "<p>No such Suggestion.</p>", status=404)
    title, body = await admin.core.db.read(lambda snap: (label(snap, suggestion), _suggestion_page(snap, suggestion)))
    return page(title, body)


@router.post("/suggestions/{suggestion_id}/accept")
async def accept_form(request: Request, suggestion_id: str) -> Response:
    admin = _admin(request)
    form = await request.form()
    mutation_id = str(form.get("mutation_id") or "")
    if not UUID.fullmatch(mutation_id):
        return _refused("Send the form as the Suggestion's page drew it.")
    suggestion = await _suggestion(admin, suggestion_id)
    if suggestion is None:
        return page("Not found", "<p>No such Suggestion.</p>", status=404)
    mutation = {"mutation_id": mutation_id, "op": "suggestion.accept", "suggestion_id": suggestion_id}
    edits = edits_from(form, _surface(suggestion))
    [result] = await _apply(admin, [{**mutation, "edits": edits} if edits else mutation])
    went = await admin.core.db.read(lambda snap: _where_it_went(snap, suggestion_id))
    if _outcome(result)["result"] == "applied":
        return RedirectResponse(f"/items/{went}" if went else "/inbox", status_code=303)
    link = f' <a href="/items/{escape(went)}">Open the Item</a>' if went else ""
    return page("Not accepted as sent", f"<p>{escape(_answer(result))}{link}</p><p><a href=\"/suggestions/{escape(suggestion_id)}\">Back</a></p>",
                status=409)


def _relations(snap: Snapshot, item_ref: str) -> list[dict]:
    return [json.loads(data) for (data,) in snap.execute(
        "SELECT data FROM relations WHERE json_extract(data, '$.from') = ? OR json_extract(data, '$.to') = ? ORDER BY id", (item_ref, item_ref)
    )]


def _item_page(snap: Snapshot, item: dict) -> str:
    item_id = item["id"]
    path = f"/items/{escape(item_id)}"
    relations = _relations(snap, f"item:{item_id}")
    parts = [f"<p>{escape(item['kind'])}</p>"]
    if "deleted_at" in item:
        parts.append(
            f'<p class="warning">This Item is in the trash since {escape(item["deleted_at"])}. Its Relations stay, so the Captures '
            "it rests on stay out of the Inbox until it is restored or they are purged.</p>"
        )
    columns = [(key, item[key]) for key in ("starts_at", "due_at", "all_day") if key in item] + sorted(item["fields"].items())
    parts.append("<dl>" + "".join(f"<dt>{escape(key)}</dt><dd>{escape(_shown(value))}</dd>" for key, value in columns) + "</dl>")
    if "body" in item:
        parts.append(f"<pre>{escape(item['body'])}</pre>")
    evidence = [relation for relation in relations if relation["type"] == "evidence" and relation["from"] == f"item:{item_id}"]
    parts.append("<h2>Evidence</h2>")
    parts.append(
        "".join(
            f"<p><code>{escape(relation['to'])}</code>{_originals(snap, relation['to'])}</p>"
            + (_evidence(snap, relation["locators"]) if "locators" in relation else "")
            for relation in evidence
        )
        or "<p><small>No evidence.</small></p>"
    )
    others = [relation for relation in relations if relation["type"] not in ("evidence", "tagged")]
    if others:
        parts.append("<h2>Relations</h2><ul>" + "".join(
            f"<li><code>{escape(relation['from'])}</code> {escape(relation['type'])} <code>{escape(relation['to'])}</code></li>" for relation in others
        ) + "</ul>")
    tags = []
    for relation in relations:
        if relation["type"] != "tagged":
            continue
        tag_id = relation["to"].removeprefix("tag:")
        tag = store.get(snap, EntityRef(Entity.TAG, tag_id))
        name = tag["name"] if tag else tag_id
        remove = f"{path}/tags/{escape(tag_id)}/remove"
        tags.append(f'<li>{escape(name)} <form method="post" action="{remove}">{_hidden("mutation_id", _new_id())}<button>Remove</button></form></li>')
    parts.append("<h2>Tags</h2>" + (f"<ul>{''.join(tags)}</ul>" if tags else "<p><small>No Tags.</small></p>"))
    parts.append(
        f'<form method="post" action="{path}/tags">'
        + _hidden("tag_id", _new_id()) + _hidden("tag_mutation_id", _new_id()) + _hidden("relation_mutation_id", _new_id())
        + '<label>Tag <input name="name" required></label> <label>Color <input type="color" name="color" value="#888888"></label> '
        "<button>Add</button> <small>A Tag of the same name is reused.</small></form>"
    )
    return "".join(parts)


@router.get("/items/{item_id}")
async def item_page(request: Request, item_id: str) -> Response:
    admin = _admin(request)
    item = await admin.core.db.read(lambda snap: store.get(snap, EntityRef(Entity.ITEM, item_id)))
    if item is None:
        return page("Not found", "<p>No such Item.</p>", status=404)
    return page(item["title"], await admin.core.db.read(lambda snap: _item_page(snap, item)))


def _tag_named(snap: Snapshot, name: str) -> dict | None:
    return next((tag for (data,) in snap.execute("SELECT data FROM tags ORDER BY id") if normalized((tag := json.loads(data))["name"]) == name), None)


def _ids(form: FormData, *names: str) -> list[str] | None:
    ids = [str(form.get(name) or "") for name in names]
    return ids if all(UUID.fullmatch(each) for each in ids) else None


async def _applied_or_refused(admin: Admin, batch: list[dict], to: str) -> Response:
    results = await _apply(admin, batch)
    if all(_outcome(result)["result"] == "applied" for result in results):
        return RedirectResponse(to, status_code=303)
    return page("Not done", "<ul>" + "".join(f"<li>{escape(_answer(result))}</li>" for result in results) + "</ul>", status=409)


@router.post("/items/{item_id}/tags")
async def add_tag_form(request: Request, item_id: str) -> Response:
    admin = _admin(request)
    form = await request.form()
    ids = _ids(form, "tag_id", "tag_mutation_id", "relation_mutation_id")
    name, color = normalized(str(form.get("name") or "")).strip(), str(form.get("color") or "").lower()
    if ids is None or not name or not COLOR.fullmatch(color):
        return _refused("A Tag takes a name and a #rrggbb color, sent from the Item's page.")
    if await admin.core.db.read(lambda snap: store.get(snap, EntityRef(Entity.ITEM, item_id))) is None:
        return page("Not found", "<p>No such Item.</p>", status=404)
    tag_id, tag_mutation_id, relation_mutation_id = ids
    same = await admin.core.db.read(lambda snap: _tag_named(snap, name))
    batch = [] if same else [{"mutation_id": tag_mutation_id, "op": "create", "entity": "tag", "id": tag_id, "data": {"name": name, "color": color}}]
    tag_id = same["id"] if same else tag_id
    batch.append({"mutation_id": relation_mutation_id, "op": "create", "entity": "relation", "id": f"item:{item_id}|tagged|tag:{tag_id}",
                  "data": {"from": f"item:{item_id}", "type": "tagged", "to": f"tag:{tag_id}"}})
    return await _applied_or_refused(admin, batch, f"/items/{item_id}")


@router.post("/items/{item_id}/tags/{tag_id}/remove")
async def remove_tag_form(request: Request, item_id: str, tag_id: str) -> Response:
    ids = _ids(await request.form(), "mutation_id")
    if ids is None:
        return _refused("Send the form as the Item's page drew it.")
    relation_id = f"item:{item_id}|tagged|tag:{tag_id}"
    return await _applied_or_refused(_admin(request), [{"mutation_id": ids[0], "op": "delete", "entity": "relation", "id": relation_id}],
                                     f"/items/{item_id}")


@router.post("/captures/{capture_id}/item")
async def item_of_capture_form(request: Request, capture_id: str) -> Response:
    """#9 decision 1's way out of the Inbox: the Item and its evidence Relation to the Capture, in one batch."""
    admin = _admin(request)
    form = await request.form()
    ids = _ids(form, "item_id", "item_mutation_id", "relation_mutation_id")
    kind, title = str(form.get("kind") or ""), normalized(str(form.get("title") or "")).strip()
    if ids is None or kind not in MADE_FROM_A_CAPTURE or not title:
        return _refused(f"An Item made of a Capture takes a title and one of the kinds {', '.join(MADE_FROM_A_CAPTURE)}, sent from the Inbox page.")
    if await admin.core.db.read(lambda snap: store.get(snap, EntityRef(Entity.CAPTURE, capture_id))) is None:
        return page("Not found", "<p>No such Capture.</p>", status=404)
    item_id, item_mutation_id, relation_mutation_id = ids
    batch = [
        {"mutation_id": item_mutation_id, "op": "create", "entity": "item", "id": item_id, "data": {"kind": kind, "title": title, "fields": {}}},
        {"mutation_id": relation_mutation_id, "op": "create", "entity": "relation", "id": f"item:{item_id}|evidence|capture:{capture_id}",
         "data": {"from": f"item:{item_id}", "type": "evidence", "to": f"capture:{capture_id}"}},
    ]
    return await _applied_or_refused(admin, batch, f"/items/{item_id}")
