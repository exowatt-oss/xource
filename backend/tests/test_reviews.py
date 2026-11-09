"""Access explanation and version-bound review API security/ledger tests.

Run with the real Postgres-backed suite; this module does not create an
independent schema or weaken the production authentication dependency.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.db import engine
from app.deps import get_current_user
from app.models import User
from tests.conftest import as_user
from tests.test_xource import ALICE, BOB, CAROL, _space, _upload

DAVE = "dave@example.com"


def _file(client, visibility="restricted"):
    space = _space(client, "Review space", visibility=visibility)
    node = _upload(client, space["id"], "spec.txt", b"first")["items"][0]
    return space["id"], node["id"]


def _share(client, space_id, email, role="viewer"):
    response = client.put(f"/api/spaces/{space_id}/grants", json={"email": email, "role": role})
    assert response.status_code == 200, response.text
    return response.json()


def _actions(client, node_id):
    response = client.get("/api/activity", params={"node_id": node_id})
    assert response.status_code == 200, response.text
    return [entry["action"] for entry in response.json()]


def test_access_explains_only_my_live_grants_and_hides_other_people(client):
    alice = as_user(client, ALICE)
    space_id, node_id = _file(alice, "organization")
    team = alice.post("/api/teams", json={"name": "Optics"}).json()
    assert alice.put(f"/api/teams/{team['id']}/members", json={"email": CAROL}).status_code == 200
    assert (
        alice.put(
            f"/api/spaces/{space_id}/grants", json={"team_id": team["id"], "role": "editor"}
        ).status_code
        == 200
    )
    direct = _share(alice, space_id, BOB, "editor")
    _share(alice, space_id, DAVE)

    owner = alice.get(f"/api/nodes/{node_id}/access").json()
    assert owner["role"] == "manager"
    assert {reason["source"] for reason in owner["reasons"]} >= {"admin", "direct", "organization"}
    assert len(owner["grants"]) == 4  # own manager, team, Bob and Dave

    bob = as_user(client, BOB)
    own = bob.get(f"/api/nodes/{node_id}/access").json()
    assert own["role"] == "editor"
    assert {r["source"] for r in own["reasons"]} == {"direct", "organization"}
    assert own["grants"] is None
    assert bob.get(f"/api/spaces/{space_id}/grants").status_code == 403
    assert DAVE not in str(own)
    assert team["id"] not in str(own)

    carol = as_user(client, CAROL)
    inherited = carol.get(f"/api/nodes/{node_id}/access").json()
    assert inherited["role"] == "editor" and inherited["grants"] is None
    assert any(
        reason["source"] == "team" and reason["label"] == "Optics" and reason["role"] == "editor"
        for reason in inherited["reasons"]
    )
    alice = as_user(client, ALICE)
    assert (
        alice.delete(
            f"/api/teams/{team['id']}/members/{_person_id(alice, team['id'], CAROL)}"
        ).status_code
        == 200
    )
    after = as_user(client, CAROL).get(f"/api/nodes/{node_id}/access").json()
    assert after["role"] == "viewer" and [r["source"] for r in after["reasons"]] == ["organization"]
    alice = as_user(client, ALICE)
    assert alice.delete(f"/api/spaces/{space_id}/grants/{direct['id']}").status_code == 204
    assert as_user(client, BOB).get(f"/api/nodes/{node_id}/access").json()["role"] == "viewer"


def _person_id(client, team_id, email):
    return next(
        member["user"]["id"]
        for member in client.get(f"/api/teams/{team_id}").json()["members"]
        if member["user"]["email"] == email
    )


def test_hidden_and_trashed_files_or_spaces_have_no_review_surface(client):
    alice = as_user(client, ALICE)
    space_id, node_id = _file(alice)
    unseen = as_user(client, BOB)
    for suffix in ("access", "comments", "review"):
        assert unseen.get(f"/api/nodes/{node_id}/{suffix}").status_code == 404
    assert unseen.post(f"/api/nodes/{node_id}/comments", json={"body": "no"}).status_code == 404
    assert (
        unseen.post(
            f"/api/nodes/{node_id}/approval", json={"version": 1, "approved": True}
        ).status_code
        == 404
    )
    _share(as_user(client, ALICE), space_id, BOB, "editor")
    assert as_user(client, BOB).get(f"/api/nodes/{node_id}/access").status_code == 200
    alice = as_user(client, ALICE)
    assert alice.delete(f"/api/nodes/{node_id}").status_code == 204
    for suffix in ("access", "comments", "review"):
        assert as_user(client, BOB).get(f"/api/nodes/{node_id}/{suffix}").status_code == 404
    assert alice.post(f"/api/nodes/{node_id}/restore").status_code == 200
    assert as_user(client, ALICE).delete(f"/api/spaces/{space_id}").status_code == 200
    for suffix in ("access", "comments", "review"):
        assert as_user(client, BOB).get(f"/api/nodes/{node_id}/{suffix}").status_code == 404


def test_comment_authorship_mentions_resolution_and_revocation(client):
    alice = as_user(client, ALICE)
    space_id, node_id = _file(alice)
    _share(alice, space_id, BOB, "editor")
    grant = _share(alice, space_id, CAROL)
    unrelated = _space(alice, "Private other space")
    _share(alice, unrelated["id"], DAVE)  # Known account, hidden from this review space.
    bob = as_user(client, BOB)
    response = bob.post(
        f"/api/nodes/{node_id}/comments",
        json={"body": "Please verify.\nThanks", "mentions": [CAROL, CAROL]},
    )
    assert response.status_code == 201, response.text
    comment = response.json()
    cid = comment["id"]
    assert comment["version"] == 1 and comment["author_label"] == BOB
    assert comment["body"] == "Please verify.\nThanks"
    assert comment["mentions"] == [{"user_id": _user_id(CAROL), "email": CAROL}]
    assert "review.comment" in _actions(bob, node_id)

    carol = as_user(client, CAROL)
    assert carol.get(f"/api/nodes/{node_id}/comments").json()[0]["id"] == cid
    inbox = carol.get("/api/review/mentions")
    assert inbox.status_code == 200 and [m["comment"]["id"] for m in inbox.json()] == [cid]
    assert carol.patch(f"/api/comments/{cid}", json={"resolved": True}).status_code == 403
    assert as_user(client, ALICE).get("/api/review/mentions").json() == []
    # An attempted recipient selector is ignored: this is always the caller's inbox.
    assert (
        as_user(client, ALICE)
        .get("/api/review/mentions", params={"user_id": _user_id(CAROL)})
        .json()
        == []
    )

    bob = as_user(client, BOB)
    rejection = None
    for mention in (["unknown@example.com"], [DAVE], [str(uuid4())]):
        bad = bob.post(
            f"/api/nodes/{node_id}/comments", json={"body": "not sent", "mentions": mention}
        )
        assert bad.status_code == 422
        assert "unknown@example.com" not in bad.text
        rejection = rejection or bad.json()
        assert bad.json() == rejection  # Known-but-hidden and unknown are indistinguishable.
    assert bob.post(f"/api/nodes/{node_id}/comments", json={"body": "  "}).status_code == 422
    assert (
        bob.patch(f"/api/comments/{cid}", json={"body": "edited", "resolved": True}).status_code
        == 422
    )
    resolved = bob.patch(f"/api/comments/{cid}", json={"resolved": True})
    assert resolved.status_code == 200 and resolved.json()["resolved"] is True
    assert (
        resolved.json()["body"] == comment["body"]
        and resolved.json()["author_id"] == comment["author_id"]
    )
    assert bob.put(f"/api/comments/{cid}", json={"resolved": False}).json()["resolved"] is False
    assert _actions(bob, node_id)[:3] == [
        "review.comment_reopen",
        "review.comment_resolve",
        "review.comment",
    ]

    alice = as_user(client, ALICE)
    assert alice.delete(f"/api/spaces/{space_id}/grants/{grant['id']}").status_code == 204
    carol = as_user(client, CAROL)
    assert carol.get("/api/review/mentions").json() == []
    assert carol.get(f"/api/nodes/{node_id}/comments").status_code == 404
    assert carol.patch(f"/api/comments/{cid}", json={"resolved": True}).status_code == 404

    # DB-level protections prevent a rushed writer from rewriting history.
    with pytest.raises(Exception, match="immutable"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE review_comments SET body = 'tampered'"))


def _user_id(email):
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            ).scalar_one()
        )


def test_comments_and_approval_are_bound_to_this_file_and_version(client):
    alice = as_user(client, ALICE)
    space_id, node_id = _file(alice)
    other = _upload(alice, space_id, "other.txt", b"other")["items"][0]["id"]
    assert (
        _upload(alice, space_id, "other.txt", b"other revision")["items"][0]["version"]["number"]
        == 2
    )
    _share(alice, space_id, BOB, "editor")
    _share(alice, space_id, CAROL)
    assert alice.get(f"/api/nodes/{node_id}/review").json()["status"] == "pending"
    assert alice.get(f"/api/nodes/{node_id}/review?version=2").status_code == 404
    assert alice.get(f"/api/nodes/{node_id}/comments?version=2").status_code == 404
    # v2 exists, but only on the OTHER file: version numbers are node-scoped.
    assert (
        alice.post(
            f"/api/nodes/{node_id}/comments", json={"body": "foreign", "version": 2}
        ).status_code
        == 404
    )
    assert (
        alice.post(
            f"/api/nodes/{node_id}/approval", json={"version": 2, "approved": True}
        ).status_code
        == 404
    )
    assert alice.get(f"/api/nodes/{other}/review?version=2").status_code == 200

    r = alice.post(f"/api/nodes/{node_id}/approval", json={"version": 1, "approved": True})
    assert r.status_code == 201 and r.json()["approved"] is True
    assert len(r.json()["decisions"]) == 1
    assert (
        as_user(client, BOB)
        .post(f"/api/nodes/{node_id}/approval", json={"version": 1, "approved": False})
        .status_code
        == 403
    )
    assert (
        as_user(client, CAROL)
        .post(f"/api/nodes/{node_id}/comments", json={"body": "no"})
        .status_code
        == 403
    )

    alice = as_user(client, ALICE)
    assert (
        alice.post(
            f"/api/nodes/{node_id}/comments", json={"body": "First review", "version": 1}
        ).status_code
        == 201
    )
    v2 = _upload(alice, space_id, "spec.txt", b"second")["items"][0]["version"]
    assert v2["number"] == 2
    assert alice.get(f"/api/nodes/{node_id}/comments").json() == []
    assert [c["body"] for c in alice.get(f"/api/nodes/{node_id}/comments?version=1").json()] == [
        "First review"
    ]
    current = alice.get(f"/api/nodes/{node_id}/review").json()
    assert (
        current["version"] == 2 and current["status"] == "pending" and current["approved"] is False
    )
    old = alice.get(f"/api/nodes/{node_id}/review?version=1").json()
    assert old["approved"] and old["current_version"] == 2
    assert (
        alice.post(
            f"/api/nodes/{node_id}/approval", json={"version": 1, "approved": False}
        ).status_code
        == 201
    )
    prior = alice.get(f"/api/nodes/{node_id}/review?version=1").json()
    assert prior["status"] == "not_approved" and len(prior["decisions"]) == 2
    assert prior["decisions"][0]["approved"] is False and prior["decisions"][1]["approved"] is True
    assert alice.get(f"/api/nodes/{node_id}/review").json()["status"] == "pending"
    assert "review.approve" in _actions(alice, node_id) and "review.unapprove" in _actions(
        alice, node_id
    )
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE review_approval_events SET approved = true"))


def test_bot_identity_is_read_only_even_when_space_is_public(client):
    alice = as_user(client, ALICE)
    _, node_id = _file(alice, visibility="organization")
    comment = alice.post(f"/api/nodes/{node_id}/comments", json={"body": "Human only"}).json()
    bot = User(id=uuid4(), email="bot@fluxbots.example.com", is_admin=False)
    bot.set_gateway_context(actor="fluxbot:test", scope="read_write")
    client.app.dependency_overrides[get_current_user] = lambda: bot
    try:
        assert client.get(f"/api/nodes/{node_id}/access").json()["role"] == "viewer"
        assert client.get(f"/api/nodes/{node_id}/comments").status_code == 200
        assert client.post(f"/api/nodes/{node_id}/comments", json={"body": "no"}).status_code == 403
        assert (
            client.patch(f"/api/comments/{comment['id']}", json={"resolved": True}).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/nodes/{node_id}/approval", json={"version": 1, "approved": True}
            ).status_code
            == 403
        )
    finally:
        client.app.dependency_overrides.pop(get_current_user, None)


def test_team_mentions_follow_live_membership_and_deleted_teams(client):
    alice = as_user(client, ALICE)
    space_id, node_id = _file(alice)
    team = alice.post("/api/teams", json={"name": "Reviewers"}).json()
    tid = team["id"]
    assert alice.put(f"/api/teams/{tid}/members", json={"email": CAROL}).status_code == 200
    assert (
        alice.put(
            f"/api/spaces/{space_id}/grants", json={"team_id": tid, "role": "viewer"}
        ).status_code
        == 200
    )
    _share(alice, space_id, BOB, "editor")
    recipient_id = _person_id(alice, tid, CAROL)
    bob = as_user(client, BOB)
    response = bob.post(
        f"/api/nodes/{node_id}/comments",
        json={"body": "Could you review?", "mentions": [recipient_id]},
    )
    assert response.status_code == 201, response.text
    carol = as_user(client, CAROL)
    assert len(carol.get("/api/review/mentions").json()) == 1

    alice = as_user(client, ALICE)
    assert alice.delete(f"/api/teams/{tid}").status_code == 200
    carol = as_user(client, CAROL)
    assert carol.get("/api/review/mentions").json() == []
    assert carol.get(f"/api/nodes/{node_id}/access").status_code == 404
    alice = as_user(client, ALICE)
    assert alice.post(f"/api/teams/{tid}/restore").status_code == 200
    assert len(as_user(client, CAROL).get("/api/review/mentions").json()) == 1
    alice = as_user(client, ALICE)
    assert alice.delete(f"/api/teams/{tid}/members/{recipient_id}").status_code == 200
    assert as_user(client, CAROL).get("/api/review/mentions").json() == []
