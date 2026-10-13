"""Application state in PostgreSQL.

Runs and everything they produced, the review decisions made on them, an outbox of
approved actions, the effects those actions had, and an audit log that the database
itself refuses to change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

metadata = MetaData()


def _now() -> Column[datetime]:
    return Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())


runs = Table(
    "runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("question", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("disposition", Text),
    Column("stop_reason", Text),
    Column("fallback", Text),
    Column("error", Text),
    Column("checks", JSONB),
    Column("usage", JSONB),
    Column("duration_ms", Float),
    _now(),
    Column("finished_at", DateTime(timezone=True)),
    CheckConstraint("status IN ('running', 'completed', 'failed')", name="runs_status"),
)

run_steps = Table(
    "run_steps",
    metadata,
    Column("run_id", Text, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("node", Text, nullable=False),
    Column("started_ms", Float, nullable=False),
    Column("duration_ms", Float, nullable=False),
    Column("detail", JSONB, nullable=False),
    PrimaryKeyConstraint("run_id", "seq"),
)

tool_calls = Table(
    "tool_calls",
    metadata,
    Column("run_id", Text, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
    Column("call_id", Text, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("tool", Text, nullable=False),
    Column("arguments", JSONB, nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("result", JSONB),
    Column("error", Text),
    Column("duration_ms", Float, nullable=False),
    PrimaryKeyConstraint("run_id", "call_id"),
)

findings = Table(
    "findings",
    metadata,
    Column("run_id", Text, ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True),
    Column("status", Text, nullable=False),
    Column("answer", Text, nullable=False),
    Column("confidence", Text, nullable=False),
    Column("numbers", JSONB, nullable=False),
    Column("caveats", JSONB, nullable=False),
    Column("state", Text, nullable=False),
    Column("decided_by", Text),
    Column("decided_at", DateTime(timezone=True)),
    Column("decision_reason", Text),
    CheckConstraint(
        "state IN ('published', 'held', 'rejected', 'unanswerable')", name="findings_state"
    ),
)

proposed_actions = Table(
    "proposed_actions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("run_id", Text, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
    Column("proposal_id", Text, nullable=False),
    Column("tool_call_id", Text, nullable=False),
    Column("action_type", Text, nullable=False),
    Column("arguments", JSONB, nullable=False),
    Column("reason", Text, nullable=False),
    Column("risk", Text, nullable=False),
    Column("state", Text, nullable=False),
    _now(),
    Column("decided_by", Text),
    Column("decided_at", DateTime(timezone=True)),
    Column("decision_reason", Text),
    Column("executed_at", DateTime(timezone=True)),
    Column("error", Text),
    UniqueConstraint("run_id", "proposal_id"),
    CheckConstraint(
        "state IN ('pending', 'approved', 'rejected', 'executed', 'failed')",
        name="proposed_actions_state",
    ),
)

outbox = Table(
    "outbox",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("action_id", Text, ForeignKey("proposed_actions.id"), nullable=False, unique=True),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("state", Text, nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("available_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_error", Text),
    _now(),
    Column("processed_at", DateTime(timezone=True)),
    CheckConstraint("state IN ('queued', 'done', 'failed')", name="outbox_state"),
)

# The downstream system of record. In a real deployment this is the payments or
# supplier system, reached over its API with the idempotency key; here it is a table,
# so the at-most-once property can be tested rather than asserted.
action_effects = Table(
    "action_effects",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("action_id", Text, nullable=False),
    Column("action_type", Text, nullable=False),
    Column("arguments", JSONB, nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("at", DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")),
    Column("actor", Text, nullable=False),
    Column("event", Text, nullable=False),
    Column("run_id", Text),
    Column("subject", Text),
    Column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
)

_APPEND_ONLY = (
    """
    CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log is append-only (% refused)', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END
    $$
    """,
    """
    CREATE OR REPLACE TRIGGER audit_log_no_change
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()
    """,
    """
    CREATE OR REPLACE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only()
    """,
    "CREATE INDEX IF NOT EXISTS audit_log_run ON audit_log (run_id, id)",
    "CREATE INDEX IF NOT EXISTS proposed_actions_pending ON proposed_actions (state, created_at)",
    "CREATE INDEX IF NOT EXISTS outbox_queued ON outbox (available_at) WHERE state = 'queued'",
)


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_size=10, max_overflow=5, pool_pre_ping=True)


async def migrate(engine: AsyncEngine) -> None:
    """Create the schema if it is missing. Idempotent; safe on every start."""
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(727274)"))
        await conn.run_sync(metadata.create_all)
        for statement in _APPEND_ONLY:
            await conn.execute(text(statement))
