"""End-to-end tests for Xource's storage, versioning, sharing and audit log.

They hit the real local Postgres, and local disk
when it is running (an in-memory object store otherwise).
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from tests.conftest import as_user

ALICE = "alice@example.org"
BOB = "bob@example.org"
CAROL = "carol@example.org"


@pytest.fixture()
def alice(client: TestClient) -> TestClient:
    return as_user(client, ALICE)


def _space(c: TestClient, name="Engineering", visibility="restricted") -> dict:
    r = c.post("/api/spaces", json={"name": name, "visibility": visibility})
    assert r.status_code == 201, r.text
    return r.json()


def _upload(c: TestClient, space_id, name, data: bytes, parent_id=None, message=None):
    form = {}
    if parent_id:
        form["parent_id"] = parent_id
    if message:
        form["message"] = message
    r = c.post(
        f"/api/spaces/{space_id}/files",
        files=[("files", (name, io.BytesIO(data), "application/octet-stream"))],
        data=form,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_personal_drive_created_on_first_list(alice):
    spaces = alice.get("/api/spaces").json()
    assert len(spaces) == 1
    assert spaces[0]["is_personal"] and spaces[0]["name"] == "My Drive"
    assert spaces[0]["my_role"] == "manager"
    # idempotent
    assert alice.get("/api/spaces/drive").json()["id"] == spaces[0]["id"]


def test_upload_versioning_and_dedup(alice):
    space = _space(alice)
    first = _upload(alice, space["id"], "spec.txt", b"rev A", message="first draft")
    assert first["created"] == 1
    node = first["items"][0]
    assert node["version"]["number"] == 1 and node["version"]["message"] == "first draft"
    assert node["version"]["content_type"] == "text/plain"

    same = _upload(alice, space["id"], "spec.txt", b"rev A")
    assert same["unchanged"] == 1 and same["items"][0]["version"]["number"] == 1

    second = _upload(alice, space["id"], "SPEC.txt", b"rev B")  # case-insensitive match
    assert second["versioned"] == 1 and second["items"][0]["id"] == node["id"]
    assert second["items"][0]["version"]["number"] == 2

    versions = alice.get(f"/api/nodes/{node['id']}/versions").json()
    assert [v["number"] for v in versions] == [2, 1]

    assert alice.get(f"/api/nodes/{node['id']}/content").content == b"rev B"
    assert alice.get(f"/api/nodes/{node['id']}/content?version=1").content == b"rev A"

    # Restore v1 → v3 with v1's bytes; history only grows
    r = alice.post(f"/api/nodes/{node['id']}/versions/1/restore")
    assert r.status_code == 200
    assert r.json()["version"]["number"] == 3 and r.json()["version"]["restored_from"] == 1
    assert alice.get(f"/api/nodes/{node['id']}/content").content == b"rev A"

    # Same bytes in another file reuse one blob
    _upload(alice, space["id"], "copy.txt", b"rev A")
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from blobs")).scalar() == 2


def test_folder_upload_paths_and_browse(alice):
    space = _space(alice)
    _upload(alice, space["id"], "drawings/rev-b/plate.dxf", b"dxf")
    root = alice.get(f"/api/spaces/{space['id']}/children").json()
    assert [i["name"] for i in root["items"]] == ["drawings"]
    drawings = root["items"][0]
    sub = alice.get(f"/api/spaces/{space['id']}/children?parent_id={drawings['id']}").json()
    assert sub["path"][-1]["name"] == "drawings"
    assert sub["items"][0]["name"] == "rev-b" and sub["items"][0]["kind"] == "folder"

    # A folder can't be overwritten by a file
    r = alice.post(
        f"/api/spaces/{space['id']}/files",
        files=[("files", ("drawings", io.BytesIO(b"x"), "text/plain"))],
    )
    assert r.status_code == 409

    # Path traversal is rejected
    r = alice.post(
        f"/api/spaces/{space['id']}/files",
        files=[("files", ("../evil.txt", io.BytesIO(b"x"), "text/plain"))],
    )
    assert r.status_code == 422


def test_move_rename_and_cycle_guard(alice):
    space = _space(alice)
    a = alice.post(f"/api/spaces/{space['id']}/folders", json={"name": "A"}).json()
    b = alice.post(
        f"/api/spaces/{space['id']}/folders", json={"name": "B", "parent_id": a["id"]}
    ).json()
    f = _upload(alice, space["id"], "note.md", b"# hi", parent_id=a["id"])["items"][0]

    assert alice.patch(f"/api/nodes/{a['id']}", json={"parent_id": b["id"]}).status_code == 409
    r = alice.patch(f"/api/nodes/{f['id']}", json={"parent_id": b["id"], "name": "readme.md"})
    assert r.status_code == 200 and r.json()["parent_id"] == b["id"]
    detail = alice.get(f"/api/nodes/{f['id']}").json()
    assert [c["name"] for c in detail["path"]] == ["Engineering", "A", "B", "readme.md"]
    assert (
        alice.patch(f"/api/nodes/{f['id']}", json={"move_to_root": True}).json()["parent_id"]
        is None
    )


def test_trash_and_restore_batch(alice):
    space = _space(alice)
    folder = alice.post(f"/api/spaces/{space['id']}/folders", json={"name": "Old"}).json()
    f = _upload(alice, space["id"], "x.txt", b"x", parent_id=folder["id"])["items"][0]

    assert alice.delete(f"/api/nodes/{folder['id']}").status_code == 204
    assert alice.get(f"/api/spaces/{space['id']}/children").json()["items"] == []
    trash = alice.get(f"/api/spaces/{space['id']}/trash").json()
    assert [t["name"] for t in trash] == ["Old"]  # child is not listed separately

    assert alice.post(f"/api/nodes/{folder['id']}/restore").status_code == 200
    assert alice.get(f"/api/nodes/{f['id']}").json()["deleted_at"] is None


def test_sharing_roles(client):
    alice = as_user(client, ALICE)
    space = _space(alice)
    _upload(alice, space["id"], "secret.txt", b"s")

    bob = as_user(client, BOB)
    assert bob.get(f"/api/spaces/{space['id']}").status_code == 404  # invisible

    alice = as_user(client, ALICE)
    r = alice.put(f"/api/spaces/{space['id']}/grants", json={"email": BOB, "role": "viewer"})
    assert r.status_code == 200, r.text

    bob = as_user(client, BOB)
    assert bob.get(f"/api/spaces/{space['id']}").json()["my_role"] == "viewer"
    r = bob.post(f"/api/spaces/{space['id']}/folders", json={"name": "nope"})
    assert r.status_code == 403
    assert bob.get("/api/search?q=secret").json()[0]["name"] == "secret.txt"

    # Cannot remove the last manager
    alice = as_user(client, ALICE)
    grants = alice.get(f"/api/spaces/{space['id']}/grants").json()
    mine = next(g for g in grants if g["role"] == "manager")
    assert alice.delete(f"/api/spaces/{space['id']}/grants/{mine['id']}").status_code == 409

    # Reject invalid email addresses
    r = alice.put(f"/api/spaces/{space['id']}/grants", json={"email": "not-an-email"})
    assert r.status_code == 422


def test_team_grants(client):
    alice = as_user(client, ALICE)
    team = alice.post("/api/teams", json={"name": "Thermal"}).json()
    assert team["my_role"] == "owner" and team["slug"] == "thermal"
    alice.put(f"/api/teams/{team['id']}/members", json={"email": CAROL, "role": "member"})
    space = _space(alice, "Thermal tests")
    alice.put(f"/api/spaces/{space['id']}/grants", json={"team_id": team["id"], "role": "editor"})

    carol = as_user(client, CAROL)
    assert carol.get(f"/api/spaces/{space['id']}").json()["my_role"] == "editor"
    _upload(carol, space["id"], "run-12.csv", b"t,temp\n0,20\n")

    # Members cannot manage the team; the last owner cannot leave
    assert carol.put(f"/api/teams/{team['id']}/members", json={"email": BOB}).status_code == 403
    alice = as_user(client, ALICE)
    me = next(
        m
        for m in alice.get(f"/api/teams/{team['id']}").json()["members"]
        if m["user"]["email"] == ALICE
    )
    assert alice.delete(f"/api/teams/{team['id']}/members/{me['user']['id']}").status_code == 409


def test_organization_visibility(client):
    alice = as_user(client, ALICE)
    space = _space(alice, "Handbook", visibility="organization")
    bob = as_user(client, BOB)
    assert bob.get(f"/api/spaces/{space['id']}").json()["my_role"] == "viewer"
    assert any(s["id"] == space["id"] for s in bob.get("/api/spaces").json())


def test_audit_log_is_append_only(alice):
    space = _space(alice)
    _upload(alice, space["id"], "a.txt", b"1")
    _upload(alice, space["id"], "a.txt", b"2")
    events = alice.get(f"/api/activity?space_id={space['id']}").json()
    assert [e["action"] for e in events] == ["file.version", "file.upload", "space.create"]
    assert events[0]["actor_label"] == ALICE

    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as conn:
            conn.execute(text("update audit_events set summary = 'tampered'"))


def test_preview_is_safe(alice):
    space = _space(alice)
    page = _upload(alice, space["id"], "x.html", b"<script>alert(1)</script>")["items"][0]
    r = alice.get(f"/api/nodes/{page['id']}/content?disposition=inline")
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["x-content-type-options"] == "nosniff"

    blob = _upload(alice, space["id"], "x.bin", b"\x00\x01")["items"][0]
    r = alice.get(f"/api/nodes/{blob['id']}/content?disposition=inline")
    assert r.headers["content-disposition"].startswith("attachment")


def test_stats_and_recent(alice):
    space = _space(alice)
    _upload(alice, space["id"], "a.txt", b"hello")
    _upload(alice, space["id"], "b.txt", b"hello")
    stats = alice.get(f"/api/stats?space_id={space['id']}").json()
    assert stats["files"] == 2 and stats["logical_bytes"] == 10 and stats["stored_bytes"] == 5
    assert len(alice.get("/api/recent").json()) == 2
