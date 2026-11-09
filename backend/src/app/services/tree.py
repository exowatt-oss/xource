"""Tree operations shared by the HTTP routers (and, later, importers such as
external import adapters).

Everything here works inside the caller's transaction and never commits.
"""

from __future__ import annotations

import mimetypes
import re
import zlib
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models import Blob, FileVersion, Node, Space, SpaceGrant, User
from app.services.audit import actor_label

_BAD_NAME = re.compile(r"[\x00-\x1f/\\]")


def now() -> datetime:
    return datetime.now(UTC)


def clean_name(name: str) -> str:
    name = name.strip()
    if not name or name in (".", "..") or _BAD_NAME.search(name):
        raise HTTPException(422, f"Invalid name: {name!r}")
    return name[:255]


def slugify(text: str, max_len: int = 64) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "item")[:max_len]


def unique_slug(db: Session, model, base: str, max_len: int) -> str:
    base = slugify(base, max_len - 4)
    slug, n = base, 2
    while db.scalar(select(model.id).where(model.slug == slug)) is not None:
        slug, n = f"{base}-{n}", n + 1
    return slug


def guess_type(name: str, declared: str | None) -> str:
    if declared and declared not in ("application/octet-stream", ""):
        return declared[:255]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def find_child(db: Session, space_id: UUID, parent_id: UUID | None, name: str) -> Node | None:
    parent_clause = Node.parent_id.is_(None) if parent_id is None else Node.parent_id == parent_id
    return db.scalar(
        select(Node).where(
            Node.space_id == space_id,
            parent_clause,
            func.lower(Node.name) == name.lower(),
            Node.deleted_at.is_(None),
        )
    )


def require_folder(db: Session, space_id: UUID, folder_id: UUID | None) -> Node | None:
    if folder_id is None:
        return None
    folder = db.get(Node, folder_id)
    if (
        folder is None
        or folder.space_id != space_id
        or folder.kind != "folder"
        or folder.deleted_at is not None
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    return folder


def make_folder(
    db: Session, user: User, space_id: UUID, parent_id: UUID | None, name: str
) -> tuple[Node, bool]:
    """Return the folder (creating it if needed) and whether it was created."""
    name = clean_name(name)
    existing = find_child(db, space_id, parent_id, name)
    if existing is not None:
        if existing.kind != "folder":
            raise HTTPException(status.HTTP_409_CONFLICT, f"A file named {name!r} already exists")
        return existing, False
    node = Node(
        space_id=space_id,
        parent_id=parent_id,
        kind="folder",
        name=name,
        created_by_id=None if user.actor else user.id,
    )
    db.add(node)
    db.flush()
    touch(db, parent_id, space_id)
    return node, True


def ensure_path(
    db: Session, user: User, space_id: UUID, parent_id: UUID | None, parts: list[str]
) -> UUID | None:
    """mkdir -p: walk/create ``parts`` under ``parent_id``; return the leaf id."""
    for part in parts:
        folder, _ = make_folder(db, user, space_id, parent_id, part)
        parent_id = folder.id
    return parent_id


def next_version_number(db: Session, node_id: UUID) -> int:
    return (
        db.scalar(select(func.max(FileVersion.number)).where(FileVersion.node_id == node_id)) or 0
    ) + 1


def upsert_blob(db: Session, sha256: str, size: int, content_type: str, key: str) -> Blob:
    from sqlalchemy.dialects.postgresql import insert
    from sqlalchemy.exc import DBAPIError

    try:
        db.execute(
            insert(Blob)
            .values(
                sha256=sha256,
                size=size,
                content_type=content_type,
                storage_key=key,
            )
            .on_conflict_do_nothing(index_elements=[Blob.sha256])
        )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) not in {"55P03", "40P01"}:
            raise
        db.rollback()
        raise HTTPException(503, "Storage metadata is busy; please retry shortly") from exc
    return db.get(Blob, sha256)


def write_file(
    db: Session,
    user: User,
    space_id: UUID,
    parent_id: UUID | None,
    name: str,
    blob: Blob,
    content_type: str,
    message: str | None,
    *,
    node: Node | None = None,
    restored_from: int | None = None,
) -> tuple[Node, str]:
    """Create a file or append a version. Returns (node, outcome).

    outcome is ``created``, ``versioned``, or ``unchanged`` (same bytes as the
    current version — git would call this "nothing to commit").
    """
    # Serialize version allocation and quota even when quotas are disabled.
    # External reads/scans must already be finished before this short lock.
    from app.services.safeguards import enforce_quota, lock_write_space

    expected = (node.space_id, node.parent_id, node.name, node.current_version_id) if node else None
    lock_write_space(db, space_id)
    if node is None:
        name = clean_name(name)
        node = find_child(db, space_id, parent_id, name)
        if node is not None and node.kind == "folder":
            raise HTTPException(status.HTTP_409_CONFLICT, f"A folder named {name!r} already exists")
        expected = (
            (node.space_id, node.parent_id, node.name, node.current_version_id) if node else None
        )
    if node is not None:
        from sqlalchemy.exc import DBAPIError

        try:
            node = db.scalar(
                select(Node)
                .where(Node.id == node.id)
                .with_for_update(nowait=True, of=Node)
                .execution_options(populate_existing=True)
            )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) not in {"55P03", "40P01"}:
                raise
            db.rollback()
            raise HTTPException(503, "This file is busy; please retry shortly") from exc
        if node is None or node.deleted_at is not None:
            raise HTTPException(404, "Item not found")
        if (node.space_id, node.parent_id, node.name, node.current_version_id) != expected:
            raise HTTPException(409, "Local file changed during write; reopen and retry")
    created = node is None
    if created:
        node = Node(
            space_id=space_id,
            parent_id=parent_id,
            kind="file",
            name=name,
            created_by_id=None if user.actor else user.id,
        )
        db.add(node)
        db.flush()
    elif (
        restored_from is None
        and node.current_version is not None
        and node.current_version.blob_sha256 == blob.sha256
    ):
        return node, "unchanged"

    enforce_quota(db, space_id, blob.size)
    version = FileVersion(
        node_id=node.id,
        number=next_version_number(db, node.id),
        blob_sha256=blob.sha256,
        size=blob.size,
        content_type=content_type,
        message=(message or None),
        restored_from=restored_from,
        author_id=None if user.actor else user.id,
        author_label=actor_label(user),
    )
    db.add(version)
    db.flush()
    node.current_version_id = version.id
    node.current_version = version
    node.updated_at = now()
    touch(db, node.parent_id, space_id)
    return node, "created" if created else "versioned"


def touch(db: Session, folder_id: UUID | None, space_id: UUID) -> None:
    """Bump modified times up the tree so folders sort by recent activity."""
    ts = now()
    seen: set[UUID] = set()
    while folder_id is not None and folder_id not in seen:
        seen.add(folder_id)
        folder = db.get(Node, folder_id)
        if folder is None:
            break
        folder.updated_at = ts
        folder_id = folder.parent_id
    db.execute(update(Space).where(Space.id == space_id).values(updated_at=ts))


_TREE_LOCK_CLASS = zlib.crc32(b"xit:tree-structure") - (1 << 31)  # int4 range


def lock_tree(db: Session, space_id: UUID) -> None:
    """Serialize changes to one space's folder structure until commit.

    Moves, trash, restore and purge all take this transaction-scoped
    advisory lock, so two moves cannot each see the other's folder as a
    valid destination and together make a cycle, and a purge cannot run
    against a tree that is being restored or moved into underneath it.
    Uploads keep their own, shorter space-row lock.
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(:cls, :key)"),
        {"cls": _TREE_LOCK_CLASS, "key": zlib.crc32(space_id.bytes) - (1 << 31)},
    )


def lock_trashed_subtree(db: Session, node_ids: list[UUID]) -> list[Node]:
    """Lock these rows and insist every one is still in the trash.

    Called under :func:`lock_tree` right before a purge: the admin's
    decision was made against a listing that may be stale (someone can
    have restored the item, or moved something live into the folder, in
    between). Only a subtree whose rows are *all* trashed at this moment,
    while locked, may be destroyed.
    """
    if not node_ids:
        return []
    try:
        rows = (
            db.execute(
                select(Node)
                .where(Node.id.in_(node_ids))
                .with_for_update(of=Node)
                .execution_options(populate_existing=True)
            )
            .unique()
            .scalars()
            .all()
        )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) not in {"55P03", "40P01"}:
            raise
        db.rollback()
        raise HTTPException(503, "These items are busy; please retry shortly") from exc
    live = [n for n in rows if n.deleted_at is None]
    if live or len(rows) != len(set(node_ids)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Something in there is no longer in the trash; refresh and try again",
        )
    return rows


def path_of(db: Session, node: Node | None) -> list[Node]:
    """Ancestors from the space root down to (and including) ``node``."""
    chain: list[Node] = []
    seen: set[UUID] = set()
    while node is not None and node.id not in seen:
        seen.add(node.id)
        chain.append(node)
        node = db.get(Node, node.parent_id) if node.parent_id else None
    return list(reversed(chain))


def descendant_ids(db: Session, node_id: UUID, *, live_only: bool = True) -> list[UUID]:
    cte = select(Node.id).where(Node.id == node_id).cte("tree", recursive=True)
    child = select(Node.id).where(Node.parent_id == cte.c.id)
    if live_only:
        child = child.where(Node.deleted_at.is_(None))
    cte = cte.union(child)
    return list(db.scalars(select(cte.c.id)))


def trash(db: Session, user: User, node: Node) -> int:
    ids = descendant_ids(db, node.id)
    ts = now()
    db.execute(
        update(Node)
        .where(Node.id.in_(ids), Node.deleted_at.is_(None))
        .values(deleted_at=ts, deleted_by_id=None if user.actor else user.id)
    )
    touch(db, node.parent_id, node.space_id)
    return len(ids)


def restore(db: Session, node: Node, *, fallback_parent_id: UUID | None = None) -> int:
    """Bring back the batch trashed together with ``node``.

    If its folder is gone, it lands in ``fallback_parent_id`` (a confined
    member's own folder) or else the space root."""
    if node.deleted_at is None:
        return 0
    if node.parent_id is not None:
        parent = db.get(Node, node.parent_id)
        if parent is None or parent.deleted_at is not None:
            node.parent_id = fallback_parent_id  # parent is gone
            db.flush()  # the session doesn't autoflush, and expire() below would drop it
    if find_child(db, node.space_id, node.parent_id, node.name) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Something named {node.name!r} already exists there; rename it first",
        )
    batch = node.deleted_at
    ids = descendant_ids(db, node.id, live_only=False)
    db.execute(
        update(Node)
        .where(Node.id.in_(ids), Node.deleted_at == batch)
        .values(deleted_at=None, deleted_by_id=None)
    )
    db.expire(node)
    touch(db, node.parent_id, node.space_id)
    return len(ids)


def purge(db: Session, node_ids: list[UUID]) -> dict[str, int]:
    """Permanently delete nodes and their versions — the one exception to
    "history only grows", for Xource admins. Pass every descendant too.

    The rows are locked and re-checked here (:func:`lock_trashed_subtree`),
    so a restore or move that landed after the admin looked makes the purge
    refuse rather than destroy live files. ``file_versions`` has a trigger
    that rejects any DELETE unless this transaction has set
    ``xource.purge = on`` *and* the owning node is trashed; this is the only
    code path that sets it.

    External links cascade with the nodes; repo links and runs pointing at a
    purged folder lose it (``SET NULL``). Blobs are left alone: they are
    content-addressed, possibly shared, and there is no garbage collector.
    The caller writes the audit event and commits.
    """
    if not node_ids:
        return {"items": 0, "versions": 0}
    lock_trashed_subtree(db, node_ids)
    db.execute(text("SET LOCAL xource.purge = 'on'"))
    # Drop the node -> current version pointers first so the cascade has no cycle.
    db.execute(
        update(Node)
        .where(Node.id.in_(node_ids))
        .values(current_version_id=None)
        .execution_options(synchronize_session=False)
    )
    versions = db.execute(
        delete(FileVersion)
        .where(FileVersion.node_id.in_(node_ids))
        .execution_options(synchronize_session=False)
    ).rowcount
    items = db.execute(
        delete(Node).where(Node.id.in_(node_ids)).execution_options(synchronize_session=False)
    ).rowcount
    db.expire_all()
    return {"items": items, "versions": versions}


def personal_space(db: Session, user: User) -> Space:
    space = db.scalar(select(Space).where(Space.owner_user_id == user.id))
    if space is None:
        local = user.email.split("@")[0]
        space = Space(
            slug=unique_slug(db, Space, f"drive-{local}", 80),
            name="My Drive",
            description=f"Personal drive of {user.name or user.email}",
            visibility="restricted",
            owner_user_id=user.id,
            created_by_id=user.id,
        )
        db.add(space)
        db.flush()
        db.add(
            SpaceGrant(
                space_id=space.id,
                principal_type="user",
                principal_id=user.id,
                role="manager",
                granted_by_id=user.id,
            )
        )
        db.flush()
    return space


def user_by_email(db: Session, email: str, *, create: bool = True) -> User:
    """Look up a person by email, creating a placeholder row for someone who
    has not signed in yet (filled in on their first sign-in)."""
    email = email.strip().lower()
    local, at, domain = email.partition("@")
    if (
        len(email) > 254
        or not at
        or not domain or "." not in domain
        or not local
        or any(c.isspace() or c == "@" for c in local)
    ):
        raise HTTPException(422, "Use a valid email address")
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        if not create:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user")
        user = User(email=email, is_admin=False)
        db.add(user)
        db.flush()
    return user
