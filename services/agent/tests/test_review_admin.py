"""The Inbox and Suggestion review in the local UI (#9 PR3), over the admin app as test_admin serves it: every form is
submitted as a browser would, Origin and all, and every write goes through the Mutation applier."""

import json
import re
import unicodedata
import uuid

import pytest
from test_admin import ORIGIN, hub, log_in, source, submitted

from campuslife_hub import artifacts, inbox, jobs, mutations, purge, review_admin, store, suggestions
from campuslife_hub.jobs import JobSpec, Pipeline
from campuslife_hub.local_import import import_files
from campuslife_hub.store import Entity, EntityRef

HOMEWORK = "과제"
"""A Korean word, so NFC and NFD differ."""
TASK = {"kind": "task", "title": "Read chapter 3", "fields": {"progress": "inbox"}}
EVENT = {"kind": "event", "title": "Club fair", "starts_at": "2026-03-10T01:00:00.000Z", "fields": {"category": "club"}}
MUTATION_ID = re.compile(r'name="mutation_id" value="([0-9a-f-]{36})"')


def new_id():
    return str(uuid.uuid4())


async def imported(admin, tmp_path, name="handout.pdf", content=b"%PDF synthetic handout"):
    """A Capture the local import took in, and its one Attachment."""
    done = await import_files(admin.core, [source(tmp_path, name, content)])
    attachment = await admin.core.db.read(
        lambda snap: snap.execute("SELECT data FROM attachments WHERE capture_id = ?", (done.capture_id,)).fetchone()[0])
    return done.capture_id, json.loads(attachment)


async def an_artifact(admin, capture_id, sha, pipeline="suggest-1"):
    content = (await admin.core.blobs.put_bytes(new_id().encode())).sha256
    spec = JobSpec("suggest", f"capture:{capture_id}", (sha,), {})
    job = await admin.core.db.write(lambda tx: jobs.enqueue(tx, spec, Pipeline(pipeline, "local", "m", "1")))
    return (await admin.core.db.write(lambda tx: artifacts.record(tx, job, "suggestions", content)))["id"]


async def proposed(admin, artifact_id, proposal=TASK, evidence=(), action="create_item"):
    return await admin.core.db.write(lambda tx: suggestions.propose(tx, artifact_id, action, proposal, list(evidence)))


async def grounds(admin, tmp_path):
    """An imported PDF and an Artifact made from it to propose from."""
    capture_id, attachment = await imported(admin, tmp_path)
    return capture_id, attachment, await an_artifact(admin, capture_id, attachment["blob_sha256"])


def page_at(sha, page=2):
    return {"v": 1, "kind": "pdf_page", "blob": sha, "page": page}


async def row(admin, entity, identifier):
    return await admin.core.db.read(lambda snap: store.get(snap, EntityRef(entity, identifier)))


async def items(admin):
    return await admin.core.db.read(lambda snap: [json.loads(data) for (data,) in snap.execute("SELECT data FROM items")])


async def test_every_page_links_to_the_inbox(tmp_path):
    async with hub(tmp_path) as (client, _):
        await log_in(client)
        page = await client.get("/")

    assert '<a href="/inbox">Inbox</a>' in page.text


async def test_the_inbox_shows_open_suggestions_with_their_evidence_and_the_unclassified_captures(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        capture_id, attachment, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id, TASK, [page_at(attachment["blob_sha256"], 2)])
        dismissed = await proposed(admin, artifact_id, {**TASK, "title": "Already dismissed"})
        await mutations.apply(admin.core.db, [{"mutation_id": new_id(), "op": "suggestion.dismiss", "suggestion_id": dismissed["id"]}],
                              admin.core.hub_device)

        page = await client.get("/inbox")

    assert page.status_code == 200
    assert "Read chapter 3" in page.text and "Already dismissed" not in page.text
    assert "p.2" in page.text and f'href="/attachments/{attachment["id"]}/original"' in page.text
    assert f'href="/suggestions/{suggestion["id"]}"' in page.text
    assert f'id="capture-{capture_id}"' in page.text


async def test_what_a_suggestion_and_a_file_name_carry_is_escaped(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, _, artifact_id = await grounds(admin, tmp_path)
        await imported(admin, tmp_path, name="<img src=x onerror=alert(2)>.txt", content=b"other")
        suggestion = await proposed(admin, artifact_id, {**TASK, "title": "<script>alert(1)</script>", "body": '"><b>bold</b>'})

        listed = await client.get("/inbox")
        detail = await client.get(f"/suggestions/{suggestion['id']}")

    for page in (listed, detail):
        assert "<script>alert(1)</script>" not in page.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
        assert "<img src=x" not in page.text
    assert '"><b>bold</b>' not in detail.text


async def test_accepting_from_the_inbox_makes_the_item_and_leads_to_it(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, attachment, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id, TASK, [page_at(attachment["blob_sha256"])])

        _, answer = await submitted(client, "/inbox", "/inbox/decide", data=await chosen_form(client, [suggestion], f"{suggestion['id']}:accept"))
        accepted = await row(admin, Entity.SUGGESTION, suggestion["id"])
        made = await client.get(f"/items/{accepted['created_item_id']}")

    assert answer.status_code == 200 and "applied" in answer.text
    assert accepted["state"] == "accepted"
    assert f'href="/items/{accepted["created_item_id"]}"' in answer.text
    assert "Read chapter 3" in made.text and "p.2" in made.text


async def chosen_form(client, chosen, decide):
    """The Inbox form as a browser sends it: each hidden field as rendered, the boxes ticked for chosen, and the
    button pressed."""
    page = await client.get("/inbox")
    hidden = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page.text))
    return {**hidden, "chosen": [suggestion["id"] for suggestion in chosen], "decide": decide}


async def test_a_batch_decides_each_chosen_suggestion_on_its_own_and_reports_each(tmp_path):
    """Not atomic: one that cannot be accepted any more does not hold back the others."""
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, _, artifact_id = await grounds(admin, tmp_path)
        first, second, third = [await proposed(admin, artifact_id, {**TASK, "title": title}) for title in ("One", "Two", "Three")]
        form = await chosen_form(client, [first, second, third], "chosen:accept")
        await mutations.apply(admin.core.db, [{"mutation_id": new_id(), "op": "suggestion.dismiss", "suggestion_id": second["id"]}],
                              admin.core.hub_device)

        _, answer = await submitted(client, "/inbox", "/inbox/decide", data=form)
        states = [(await row(admin, Entity.SUGGESTION, each["id"]))["state"] for each in (first, second, third)]

    assert states == ["accepted", "dismissed", "accepted"]
    results = re.findall(r'<tr class="result-(\w+)"><td>([^<]+)</td>', answer.text)
    assert results == [("applied", "One"), ("conflict", "Two"), ("applied", "Three")]


async def test_dismissing_chosen_suggestions_makes_no_item(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, _, artifact_id = await grounds(admin, tmp_path)
        first, second = [await proposed(admin, artifact_id, {**TASK, "title": title}) for title in ("One", "Two")]

        _, answer = await submitted(client, "/inbox", "/inbox/decide", data=await chosen_form(client, [first, second], "chosen:dismiss"))

        assert [(await row(admin, Entity.SUGGESTION, each["id"]))["state"] for each in (first, second)] == ["dismissed", "dismissed"]
        assert await items(admin) == []
    assert answer.text.count('class="result-applied"') == 2


async def test_a_form_sent_twice_is_a_duplicate_the_second_time(tmp_path):
    """The mutation_id is fixed when the page is drawn, so a second press of the same form changes nothing."""
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, _, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id)
        form = await chosen_form(client, [suggestion], f"{suggestion['id']}:accept")

        await client.post("/inbox/decide", data=form, headers=ORIGIN)
        again = await client.post("/inbox/decide", data=form, headers=ORIGIN)

        assert 'class="result-duplicate"' in again.text
        assert len(await items(admin)) == 1


async def test_edits_on_the_suggestion_page_are_normalized_and_written_with_the_accept(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, _, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id, {**TASK, "body": "first line"})
        path = f"/suggestions/{suggestion['id']}"
        page = await client.get(path)
        mutation_id = MUTATION_ID.search(page.text).group(1)

        _, answer = await submitted(client, path, f"{path}/accept", data={
            "mutation_id": mutation_id,
            "edit.title": unicodedata.normalize("NFD", f"{HOMEWORK} 3"),
            "edit.body": "first line\r\nsecond line",
            "edit.fields.progress": "inbox",
        })
        [item] = await items(admin)

    assert answer.status_code == 303 and answer.headers["location"] == f"/items/{item['id']}"
    assert item["title"] == unicodedata.normalize("NFC", f"{HOMEWORK} 3")
    assert item["body"] == "first line\nsecond line"


async def test_an_edit_may_fill_in_what_the_suggestion_left_out(tmp_path):
    """#9 decision 3: a column or a field of the kind the model did not propose; a value that is not text is JSON."""
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, _, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id, EVENT)
        path = f"/suggestions/{suggestion['id']}"
        page = await client.get(path)

        assert 'name="edit.fields.place_text"' in page.text and 'name="edit.body"' in page.text
        _, answer = await submitted(client, path, f"{path}/accept", data={
            "mutation_id": MUTATION_ID.search(page.text).group(1), "edit.fields.place_text": "Student hall", "edit.all_day": "false",
        })
        [item] = await items(admin)

    assert answer.status_code == 303
    assert item["fields"] == {"category": "club", "place_text": "Student hall"} and item["all_day"] is False


async def test_an_edit_the_item_cannot_take_is_shown_as_the_answer_and_the_suggestion_stays_open(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, _, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id)
        path = f"/suggestions/{suggestion['id']}"
        page = await client.get(path)

        _, answer = await submitted(client, path, f"{path}/accept", data={
            "mutation_id": MUTATION_ID.search(page.text).group(1), "edit.fields.progress": "someday",
        })

        assert answer.status_code == 409 and "rejected" in answer.text and "schema" in answer.text
        assert (await row(admin, Entity.SUGGESTION, suggestion["id"]))["state"] == "open"
        assert await items(admin) == []


async def test_a_patch_item_suggestion_is_edited_by_its_to_and_accepted(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, _, artifact_id = await grounds(admin, tmp_path)
        item_id = new_id()
        await mutations.apply(admin.core.db, [{"mutation_id": new_id(), "op": "create", "entity": "item", "id": item_id,
                                               "data": {**TASK, "due_at": "2026-03-16T14:59:00.000Z"}}], admin.core.hub_device)
        suggestion = await proposed(admin, artifact_id, {"entity": "item", "id": item_id, "set": {
            "due_at": {"from": "2026-03-16T14:59:00.000Z", "to": "2026-03-23T14:59:00.000Z"}}}, action="patch_item")
        path = f"/suggestions/{suggestion['id']}"
        page = await client.get(path)

        assert "2026-03-16T14:59:00.000Z" in page.text and 'name="edit.title"' not in page.text
        _, answer = await submitted(client, path, f"{path}/accept", data={
            "mutation_id": MUTATION_ID.search(page.text).group(1), "edit.due_at": "2026-03-30T14:59:00.000Z",
        })

        assert answer.status_code == 303 and answer.headers["location"] == f"/items/{item_id}"
        assert (await row(admin, Entity.ITEM, item_id))["due_at"] == "2026-03-30T14:59:00.000Z"


async def test_a_decided_suggestion_page_says_how_it_was_decided_and_offers_no_form(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, _, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id)
        await mutations.apply(admin.core.db, [{"mutation_id": new_id(), "op": "suggestion.dismiss", "suggestion_id": suggestion["id"]}],
                              admin.core.hub_device)

        page = await client.get(f"/suggestions/{suggestion['id']}")
        missing = await client.get(f"/suggestions/{new_id()}")

    assert "dismissed" in page.text and "<form" not in page.text.split("</nav>", 1)[1]
    assert missing.status_code == 404


async def test_the_item_page_adds_a_new_tag_reuses_one_by_name_and_removes_it(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        item_id = new_id()
        await mutations.apply(admin.core.db, [{"mutation_id": new_id(), "op": "create", "entity": "item", "id": item_id, "data": TASK}],
                              admin.core.hub_device)
        path = f"/items/{item_id}"

        page = await client.get(path)
        form = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page.text.split('action="' + path + '/tags"', 1)[1]))
        _, added = await submitted(client, path, f"{path}/tags", data={**form, "name": unicodedata.normalize("NFD", HOMEWORK), "color": "#3366cc"})
        [tag] = await admin.core.db.read(lambda snap: [json.loads(data) for (data,) in snap.execute("SELECT data FROM tags")])
        other_id = new_id()
        await mutations.apply(admin.core.db, [{"mutation_id": new_id(), "op": "create", "entity": "item", "id": other_id, "data": TASK}],
                              admin.core.hub_device)
        other = await client.get(f"/items/{other_id}")
        reused_form = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', other.text.split(f'action="/items/{other_id}/tags"', 1)[1]))
        await submitted(client, f"/items/{other_id}", f"/items/{other_id}/tags", data={**reused_form, "name": HOMEWORK, "color": "#000000"})
        tags = await admin.core.db.read(lambda snap: snap.execute("SELECT count(*) FROM tags").fetchone()[0])
        shown = await client.get(path)
        remove = f"{path}/tags/{tag['id']}/remove"
        _, removed = await submitted(client, path, remove, data={"mutation_id": re.search(
            rf'action="{remove}"><input type="hidden" name="mutation_id" value="([0-9a-f-]+)"', shown.text).group(1)})
        relations = await admin.core.db.read(lambda snap: [row[0] for row in snap.execute("SELECT id FROM relations")])

    assert added.status_code == 303 and removed.status_code == 303
    assert tag["name"] == unicodedata.normalize("NFC", HOMEWORK) and tag["color"] == "#3366cc"
    assert tags == 1
    assert HOMEWORK in shown.text
    assert relations == [f"item:{other_id}|tagged|tag:{tag['id']}"]


async def test_an_item_in_the_trash_says_what_it_rests_on_stays_classified(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        capture_id, _, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id, TASK, [{"v": 1, "kind": "document", "blob": (await imported_sha(admin, capture_id))}])
        await mutations.apply(admin.core.db, [{"mutation_id": new_id(), "op": "suggestion.accept", "suggestion_id": suggestion["id"]}],
                              admin.core.hub_device)
        item_id = (await row(admin, Entity.SUGGESTION, suggestion["id"]))["created_item_id"]
        await mutations.apply(admin.core.db, [{"mutation_id": new_id(), "op": "delete", "entity": "item", "id": item_id}], admin.core.hub_device)

        page = await client.get(f"/items/{item_id}")
        listed = await client.get("/inbox")

    assert "in the trash" in page.text and "stay out of the Inbox" in page.text
    assert f'id="capture-{capture_id}"' not in listed.text


async def imported_sha(admin, capture_id):
    return await admin.core.db.read(
        lambda snap: snap.execute("SELECT blob_sha256 FROM attachments WHERE capture_id = ?", (capture_id,)).fetchone()[0])


async def test_making_an_item_of_a_capture_takes_it_out_of_the_inbox(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        capture_id, _ = await imported(admin, tmp_path)
        page = await client.get("/inbox")
        action = f"/captures/{capture_id}/item"
        form = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page.text.split(f'action="{action}"', 1)[1].split("</form>", 1)[0]))

        _, answer = await submitted(client, "/inbox", action, data={**form, "kind": "note", "title": "Handout notes"})
        [item] = await items(admin)
        left = await admin.core.db.read(inbox.compute)

    assert answer.status_code == 303 and answer.headers["location"] == f"/items/{item['id']}"
    assert (item["kind"], item["title"]) == ("note", "Handout notes")
    assert left.captures == []


async def test_a_capture_whose_files_were_purged_comes_back_to_the_inbox_saying_so(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        capture_id, attachment, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id, TASK, [page_at(attachment["blob_sha256"])])
        await mutations.apply(admin.core.db, [{"mutation_id": new_id(), "op": "suggestion.accept", "suggestion_id": suggestion["id"]}],
                              admin.core.hub_device)
        assert f'id="capture-{capture_id}"' not in (await client.get("/inbox")).text

        await purge.purge_blob(admin.core.db, admin.core.blobs, attachment["blob_sha256"], hooks=admin.core.purge_hooks)
        page = await client.get("/inbox")

    assert f'id="capture-{capture_id}"' in page.text and "files were purged" in page.text


@pytest.mark.parametrize(("show", "suggestion_shown", "capture_shown"), [
    ("all", True, True), ("suggestions", True, False), ("captures", False, True), ("nonsense", True, True),
])
async def test_the_inbox_shows_one_part_or_both(tmp_path, show, suggestion_shown, capture_shown):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        capture_id, _, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id)

        page = await client.get("/inbox", params={"show": show})

    assert (f'href="/suggestions/{suggestion["id"]}"' in page.text, f'id="capture-{capture_id}"' in page.text) == (suggestion_shown, capture_shown)


@pytest.mark.parametrize(("path", "data"), [
    ("/inbox/decide", {"decide": "x:accept"}), ("/suggestions/{id}/accept", {"mutation_id": "not a uuid"}),
    ("/captures/{id}/item", {"kind": "note", "title": "t"}),
])
async def test_a_form_without_the_ids_its_page_gave_is_refused_with_nothing_written(tmp_path, path, data):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        capture_id, _, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id)

        answer = await client.post(path.format(id=suggestion["id"] if "suggestions" in path else capture_id), data=data, headers=ORIGIN)

        assert answer.status_code == 400
        assert (await row(admin, Entity.SUGGESTION, suggestion["id"]))["state"] == "open" and await items(admin) == []


async def test_a_form_posted_from_another_origin_is_refused(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, _, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id)
        form = await chosen_form(client, [suggestion], f"{suggestion['id']}:accept")

        answer = await client.post("/inbox/decide", data=form, headers={"origin": "http://evil.invalid"})

        assert answer.status_code == 403
        assert (await row(admin, Entity.SUGGESTION, suggestion["id"]))["state"] == "open"


SHA = "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae"


@pytest.mark.parametrize(("locator", "text"), [
    ({"v": 1, "kind": "pdf_page", "blob": SHA, "page": 2}, "p.2"),
    ({"v": 1, "kind": "pdf_page", "blob": SHA, "page": 2, "box": [0.1, 0.2, 0.3, 0.4]}, "p.2, part of the page"),
    ({"v": 1, "kind": "slide", "blob": SHA, "slide": 5, "shape": 3}, "slide 5, shape 3"),
    ({"v": 1, "kind": "doc_path", "blob": SHA, "heading_path": ["2", "Grading"], "block_index": 3}, "§ 2 › Grading ¶4"),
    ({"v": 1, "kind": "doc_path", "blob": SHA, "heading_path": [], "block_index": 0}, "¶1"),
    ({"v": 1, "kind": "sheet_range", "blob": SHA, "sheet": "Week 3", "a1": "B2:C4"}, "Week 3!B2:C4"),
    ({"v": 1, "kind": "sheet_range", "blob": SHA, "a1": "A1"}, "A1"),
    ({"v": 1, "kind": "media_time", "capture": "0f8fad5b-d9cb-469f-a165-70867728950e", "start_ms": 1930000, "end_ms": 1985000},
     "00:32:10–00:33:05"),
    ({"v": 1, "kind": "media_time", "capture": "0f8fad5b-d9cb-469f-a165-70867728950e", "start_ms": 3723000, "end_ms": 3723400}, "01:02:03"),
    ({"v": 1, "kind": "image_region", "blob": SHA, "box": [0, 0, 1, 1]}, "part of the image"),
    ({"v": 1, "kind": "web_snapshot", "blob": SHA, "url": "https://example.invalid/notice", "anchor": "apply"}, "https://example.invalid/notice#apply"),
    ({"v": 1, "kind": "document", "blob": SHA}, "the whole file"),
    ({"v": 2, "kind": "annotation", "blob": SHA}, "a place of kind annotation this Hub cannot show"),
])
def test_a_locator_reads_as_a_place_in_the_original(locator, text):
    """#9 decision 10: evidence is shown as text, with the original to download beside it."""
    assert review_admin.locator_text(locator) == text


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/inbox"), ("POST", "/inbox/decide"), ("GET", "/suggestions/x"), ("POST", "/suggestions/x/accept"), ("GET", "/items/x"),
    ("POST", "/items/x/tags"), ("POST", "/items/x/tags/y/remove"), ("POST", "/captures/x/item"),
])
async def test_a_review_page_without_a_session_is_refused(tmp_path, method, path):
    async with hub(tmp_path) as (client, _):
        answer = await client.request(method, path, headers=ORIGIN)

    assert answer.status_code == 401


async def test_an_edit_to_text_stays_text_though_it_reads_as_json(tmp_path):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, _, artifact_id = await grounds(admin, tmp_path)
        suggestion = await proposed(admin, artifact_id)
        path = f"/suggestions/{suggestion['id']}"
        page = await client.get(path)

        await submitted(client, path, f"{path}/accept", data={"mutation_id": MUTATION_ID.search(page.text).group(1), "edit.title": "2026"})
        [item] = await items(admin)

    assert item["title"] == "2026"


@pytest.mark.parametrize("kind", ["task", "assignment", "club_meeting"])
async def test_an_item_of_a_capture_takes_only_a_kind_that_needs_no_field(tmp_path, kind):
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        capture_id, _ = await imported(admin, tmp_path)
        page = await client.get("/inbox")
        action = f"/captures/{capture_id}/item"
        form = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page.text.split(f'action="{action}"', 1)[1].split("</form>", 1)[0]))

        _, answer = await submitted(client, "/inbox", action, data={**form, "kind": kind, "title": "Handout"})

        assert answer.status_code == 400 and await items(admin) == []


async def test_a_suggestion_says_which_it_conflicts_with_and_that_the_user_accepted_that_one(tmp_path):
    """#9 decision 9: the Hub does not choose between them; the page shows the pair."""
    async with hub(tmp_path) as (client, admin):
        await log_in(client)
        _, _, artifact_id = await grounds(admin, tmp_path)
        first = await proposed(admin, artifact_id, {**TASK, "title": "Due Monday"})
        second = await admin.core.db.write(lambda tx: suggestions.propose(
            tx, artifact_id, "create_item", {**TASK, "title": "Due Tuesday"}, [], conflicts_with=[first["id"]]))

        before = await client.get("/inbox")
        await mutations.apply(admin.core.db, [{"mutation_id": new_id(), "op": "suggestion.accept", "suggestion_id": first["id"]}],
                              admin.core.hub_device)
        after = await client.get(f"/suggestions/{second['id']}")

    assert f'Conflicts with <a href="/suggestions/{first["id"]}">Due Monday</a></p>' in before.text
    assert f'Conflicts with <a href="/suggestions/{first["id"]}">Due Monday</a> You accepted it.' in after.text
