from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TypedDict

import aiosqlite
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from action_ledger import AgentActionStore


class ConfirmationState(TypedDict, total=False):
    action_id: str
    action_version: int
    confirmed: bool
    error: str


def _compile_confirmation_graph(checkpointer: object):
    def confirm_node(state: ConfirmationState) -> dict[str, object]:
        resume = interrupt(
            {
                "action_id": state["action_id"],
                "status": "PROPOSED",
            }
        )
        if not isinstance(resume, dict) or resume.get(
            "credential_validated"
        ) is not True:
            return {
                "confirmed": False,
                "error": "确认凭证未通过服务端校验",
            }

        store = AgentActionStore()
        action = store.get_action(state["action_id"])
        if action is None:
            return {"confirmed": False, "error": "退款提议不存在"}
        confirmed = store.confirm_refund_proposal(
            action_id=state["action_id"],
            user_id=str(action["user_id"]),
            expected_version=state["action_version"],
        )
        return {
            "confirmed": confirmed,
            "error": "" if confirmed else "退款提议状态或版本已变化",
        }

    graph = StateGraph(ConfirmationState)
    graph.add_node("confirm", confirm_node)
    graph.add_edge(START, "confirm")
    graph.add_edge("confirm", END)
    return graph.compile(checkpointer=checkpointer)


def confirmation_config(action_id: str) -> dict[str, object]:
    return {"configurable": {"thread_id": f"action:{action_id}"}}


def start_confirmation_workflow(
    checkpoint_path: str | Path,
    action: dict[str, object],
) -> None:
    checkpoint_file = Path(checkpoint_path).resolve()
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(checkpoint_file, check_same_thread=False)
    graph = _compile_confirmation_graph(SqliteSaver(connection))
    try:
        graph.invoke(
            {
                "action_id": str(action["action_id"]),
                "action_version": int(action["version"]),
            },
            config=confirmation_config(str(action["action_id"])),
        )
        snapshot = graph.get_state(
            confirmation_config(str(action["action_id"]))
        )
        interrupted = any(
            getattr(task, "interrupts", ()) for task in snapshot.tasks
        )
        if not interrupted:
            raise RuntimeError("确认工作流未进入 interrupt 状态")
    finally:
        connection.close()


async def create_async_confirmation_workflow(
    checkpoint_path: str | Path,
):
    checkpoint_file = Path(checkpoint_path).resolve()
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(checkpoint_file)
    try:
        return _compile_confirmation_graph(AsyncSqliteSaver(connection))
    except Exception:
        await connection.close()
        raise


async def close_async_confirmation_workflow(graph: object) -> None:
    checkpointer = getattr(graph, "checkpointer", None)
    connection = getattr(checkpointer, "conn", None)
    if connection is not None:
        await connection.close()
