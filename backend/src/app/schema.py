"""Initial standalone schema with DB-enforced immutable history.

Run once on an empty PostgreSQL database. Future version upgrades need an
operator-managed migration (this first export does not import private migrations).
"""
from sqlalchemy import text
from app.db import Base, engine
import app.models  # noqa: F401 - register all public tables

TRIGGERS = {
    "audit_events": ("audit_events_no_update_delete", """
      BEGIN RAISE EXCEPTION 'audit_events is append-only'; END;"""),
    "file_versions": ("file_versions_no_update_delete", """
      BEGIN
        IF TG_OP = 'UPDATE' THEN RAISE EXCEPTION 'file_versions is append-only'; END IF;
        IF current_setting('xource.purge', true) IS DISTINCT FROM 'on' THEN
          RAISE EXCEPTION 'file_versions rows may only be removed by an admin purge'; END IF;
        IF EXISTS (SELECT 1 FROM nodes WHERE id = OLD.node_id AND deleted_at IS NULL) THEN
          RAISE EXCEPTION 'file_versions of a live item may not be removed'; END IF;
        RETURN OLD;
      END;"""),
    "review_comments": ("review_comments_immutable", """
      BEGIN
        IF TG_OP = 'DELETE' THEN
          IF NOT EXISTS (SELECT 1 FROM file_versions WHERE id = OLD.version_id) THEN RETURN OLD; END IF;
          RAISE EXCEPTION 'review comments cannot be deleted';
        END IF;
        IF OLD.node_id IS DISTINCT FROM NEW.node_id OR OLD.version_id IS DISTINCT FROM NEW.version_id
           OR OLD.version_number IS DISTINCT FROM NEW.version_number OR OLD.author_id IS DISTINCT FROM NEW.author_id
           OR OLD.author_label IS DISTINCT FROM NEW.author_label OR OLD.body IS DISTINCT FROM NEW.body
           OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
          RAISE EXCEPTION 'review comment content and attribution are immutable';
        END IF;
        RETURN NEW;
      END;"""),
    "review_approval_events": ("review_approvals_no_update_delete", """
      BEGIN
        IF TG_OP = 'DELETE' AND NOT EXISTS (SELECT 1 FROM file_versions WHERE id = OLD.version_id) THEN
          RETURN OLD;
        END IF;
        RAISE EXCEPTION 'review approvals are append-only';
      END;"""),
}


def initialize() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, (trigger, body) in TRIGGERS.items():
            function = trigger + "_fn"
            conn.execute(text(f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger AS $$ {body} $$ LANGUAGE plpgsql"))
            installed = conn.scalar(text("SELECT 1 FROM pg_trigger WHERE tgname = :name AND NOT tgisinternal"), {"name": trigger})
            if not installed:
                conn.execute(text(f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
                                  f"FOR EACH ROW EXECUTE FUNCTION {function}()"))


if __name__ == "__main__":
    initialize()
