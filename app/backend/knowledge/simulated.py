"""Simulated knowledge database for the demo: the §3.5 tables kept in one JSON file.

Tables: agent_conversations, agent_messages (unique conversation_id + seq) and
agent_assessments (a supplier agent's yes/no on a request). Writes are serialised with an
asyncio lock; the file is data/demo/agent_db.json (gitignored with the rest of data/demo).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.backend.knowledge.client import CONVERSATION_STATUSES, MESSAGE_TYPES
from app.backend.knowledge.context import build_context

TABLES = ("agent_conversations", "agent_messages", "agent_assessments")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SimulatedKnowledgeClient:
    def __init__(self, path: Path, profiles: dict[str, dict[str, Any]]) -> None:
        self.path = path
        self.profiles = profiles
        self.lock = asyncio.Lock()
        self.db: dict[str, list[dict[str, Any]]] = {table: [] for table in TABLES}
        if path.exists():
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.db.update({table: stored.get(table, []) for table in TABLES})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.db, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def _conversation(self, conversation_id: str) -> dict[str, Any]:
        for conversation in self.db["agent_conversations"]:
            if conversation["id"] == conversation_id:
                return conversation
        raise KeyError(conversation_id)

    def _with_messages(self, conversation: dict[str, Any]) -> dict[str, Any]:
        messages = [m for m in self.db["agent_messages"] if m["conversation_id"] == conversation["id"]]
        return {**conversation, "messages": sorted(messages, key=lambda m: m["seq"])}

    async def context(self, role, purpose, subject, query=None, viewer_id=None, limit=8):
        return build_context(self.profiles, role, purpose, subject, limit)

    async def create_conversation(self, conversation: dict[str, Any]) -> dict[str, Any]:
        row = {
            "id": str(uuid4()),
            "status": "open",
            "demo": True,
            "outcome": None,
            "typing": None,
            "created_at": now(),
            "closed_at": None,
            **conversation,
        }
        async with self.lock:
            self.db["agent_conversations"].append(row)
            self._save()
        return row

    async def append_message(self, conversation_id: str, message: dict[str, Any]) -> dict[str, Any]:
        if message.get("type") not in MESSAGE_TYPES:
            raise ValueError(f"unknown message type {message.get('type')!r}")
        async with self.lock:
            self._conversation(conversation_id)
            seq = 1 + max((m["seq"] for m in self.db["agent_messages"] if m["conversation_id"] == conversation_id), default=0)
            row = {
                "id": str(uuid4()),
                "conversation_id": conversation_id,
                "seq": seq,
                "payload": {},
                "refs": [],
                "model": None,
                "agent_version": None,
                "created_at": now(),
                **message,
            }
            self.db["agent_messages"].append(row)
            self._save()
        return row

    async def update_conversation(self, conversation_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        if "status" in fields and fields["status"] not in CONVERSATION_STATUSES:
            raise ValueError(f"unknown status {fields['status']!r}")
        async with self.lock:
            conversation = self._conversation(conversation_id)
            conversation.update(fields)
            self._save()
        return conversation

    async def close_conversation(self, conversation_id: str, status: str, outcome: dict[str, Any]) -> dict[str, Any]:
        return await self.update_conversation(
            conversation_id, {"status": status, "outcome": outcome, "typing": None, "closed_at": now()}
        )

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        try:
            return self._with_messages(self._conversation(conversation_id))
        except KeyError:
            return None

    async def list_conversations(self, company_id: str | None = None) -> list[dict[str, Any]]:
        return [
            self._with_messages(c)
            for c in self.db["agent_conversations"]
            if company_id is None or company_id in (c.get("buyer_id"), c.get("supplier_id"))
        ]

    async def record_assessment(self, assessment: dict[str, Any]) -> dict[str, Any]:
        row = {"id": str(uuid4()), "created_at": now(), **assessment}
        async with self.lock:
            self.db["agent_assessments"].append(row)
            self._save()
        return row

    async def list_assessments(self, company_id: str | None = None) -> list[dict[str, Any]]:
        return [
            a for a in self.db["agent_assessments"]
            if company_id is None or company_id in (a.get("buyer_id"), a.get("supplier_id"))
        ]
