import json
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError

from app.backend.knowledge.client import (
    KnowledgeClient,
    KnowledgeForbiddenError,
    KnowledgeInvalidError,
    KnowledgeNotFoundError,
    KnowledgeRejectedError,
    KnowledgeUnavailableError,
    translate_database_error,
    vector_literal,
)


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class FakeConnection:
    def __init__(self, engine):
        self.engine = engine

    async def execute(self, statement, params):
        self.engine.executed.append((str(statement), params))
        if self.engine.error is not None:
            raise self.engine.error
        return FakeResult(self.engine.results.pop(0) if self.engine.results else None)


class FakeEngine:
    def __init__(self, *results, error=None):
        self.results = list(results)
        self.error = error
        self.executed: list[tuple[str, dict]] = []

    @asynccontextmanager
    async def begin(self):
        yield FakeConnection(self)


class DriverError(Exception):
    def __init__(self, sqlstate: str, message: str) -> None:
        super().__init__(f"<class 'asyncpg.exceptions.RaiseError'>: {message}")
        self.sqlstate = sqlstate


def db_error(sqlstate: str, message: str = "boom") -> DBAPIError:
    return DBAPIError("select 1", {}, DriverError(sqlstate, message))


def test_vector_literal_is_pgvector_text() -> None:
    assert vector_literal(None) is None
    assert vector_literal([0.5, -0.25, 1e-9]) == "[0.5,-0.25,1e-09]"


@pytest.mark.parametrize(
    ("sqlstate", "message", "expected"),
    [
        ("42883", "function public.agent_context does not exist", KnowledgeUnavailableError),
        ("42P01", 'relation "public.kb_chunks" does not exist', KnowledgeUnavailableError),
        ("42501", "permission denied for function kb_search", KnowledgeUnavailableError),
        ("42501", "sender x is not the buyer of conversation c", KnowledgeForbiddenError),
        ("P0002", "conversation c not found", KnowledgeNotFoundError),
        ("22023", "illegal transition: accept before a quote or counter", KnowledgeRejectedError),
        ("22023", "conversation c is closed", KnowledgeRejectedError),
        ("22023", "agent_message_append: payload must be a JSON object", KnowledgeInvalidError),
        ("P0001", "custom", KnowledgeRejectedError),
        ("23505", "duplicate key", KnowledgeRejectedError),
    ],
)
def test_database_errors_are_translated(sqlstate: str, message: str, expected: type) -> None:
    error = translate_database_error(db_error(sqlstate, message), "agent_message_append")
    assert isinstance(error, expected)
    assert "<class" not in str(error)


def test_errors_without_sqlstate_are_connection_failures() -> None:
    error = translate_database_error(OperationalError("select 1", {}, OSError("refused")), "kb_search")
    assert isinstance(error, KnowledgeUnavailableError)
    assert str(error) == "Supabase PostgreSQL connection failed"


async def test_context_binds_json_vector_and_viewer() -> None:
    engine = FakeEngine(json.dumps({"purpose": "search", "refs": []}))
    client = KnowledgeClient(engine)

    pack = await client.context(role="buyer", purpose="search", subject={}, query_embedding=[1.0, 0.0],
                                query_text="9x19", viewer="buyer-1", limit=5)

    sql, params = engine.executed[0]
    assert "public.agent_context(" in sql
    assert "extensions.vector" in sql
    assert params["subject"] == "{}"
    assert params["embedding"] == "[1,0]"
    assert params["viewer"] == "buyer-1"
    assert pack == {"purpose": "search", "refs": []}


async def test_search_passes_kinds_as_array_and_returns_list() -> None:
    engine = FakeEngine([{"ref": "ted:1"}])
    client = KnowledgeClient(engine)

    hits = await client.search(query_text="cartridges 9x19", kinds=["award", "contract"])

    _, params = engine.executed[0]
    assert params["kinds"] == ["award", "contract"]
    assert params["embedding"] is None
    assert hits == [{"ref": "ted:1"}]


async def test_append_message_merges_the_sent_message_with_the_rpc_result() -> None:
    rpc_result = {"id": "8d0f6b1e-0000-0000-0000-000000000001", "conversation_id": "c-1", "seq": 3,
                  "type": "quote", "sender_role": "supplier", "status": "quoted", "previous_status": "open",
                  "ref": "msg:8d0f6b1e-0000-0000-0000-000000000001"}
    engine = FakeEngine(rpc_result)
    client = KnowledgeClient(engine)
    sent = {"type": "quote", "sender_role": "supplier", "text": "1.2 EUR",
            "payload": {"unit_price": 1.2}, "refs": ["ted:1"]}

    row = await client.append_message("c-1", sent)

    assert row["seq"] == 3
    assert row["status"] == "quoted"
    assert row["payload"] == {"unit_price": 1.2}
    assert row["refs"] == ["ted:1"]
    assert "agent_message_append" in engine.executed[0][0]
    assert json.loads(engine.executed[0][1]["message"]) == sent


async def test_missing_optional_tables_fall_back_to_defaults() -> None:
    client = KnowledgeClient(FakeEngine(error=db_error("42P01", 'relation "public.agent_mandates" does not exist')))
    assert await client.active_mandate(["buyer:x"]) is None


async def test_state_machine_violation_is_rejected() -> None:
    client = KnowledgeClient(FakeEngine(error=db_error("22023", "illegal transition: accept before a quote or counter")))
    with pytest.raises(KnowledgeRejectedError, match="accept before a quote"):
        await client.append_message("c-1", {"type": "accept"})


async def test_memory_feedback_uses_the_rpc() -> None:
    engine = FakeEngine({"ref": "memory:m-1", "helpful": 2, "unhelpful": 0})
    counters = await KnowledgeClient(engine).memory_feedback("m-1", True)
    assert "public.memory_feedback(" in engine.executed[0][0]
    assert counters == {"ref": "memory:m-1", "helpful": 2, "unhelpful": 0}
    assert await KnowledgeClient(FakeEngine(None)).memory_feedback("m-2", False) is None
