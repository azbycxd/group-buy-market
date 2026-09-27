from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, TypedDict

import aiosqlite
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from action_ledger import ActionStatus, AgentActionStore
from tools.facts import OrderFacts
from tools.facts_client import query_facts
from tools.refund_execute import (
    ExecutionCertainty,
    RefundExecutionResult,
    execute_refund,
)


class ConfirmationState(TypedDict, total=False):
    action_id: str
    action_version: int
    request_id: str
    confirmed: bool
    execution_acquired: bool
    execution_certainty: str
    result_code: str
    retry_count: int
    final_status: str
    message: str
    error: str


def _order_status(
    action: dict[str, Any],
    *,
    request_id: str,
) -> tuple[bool, str | None]:
    result = query_facts(
        path="/api/v1/agent/order/facts",
        body={"outTradeNo": str(action["out_trade_no"])},
        user_id=str(action["user_id"]),
        request_id=request_id,
        data_model=OrderFacts,
    )
    if result.get("success") is not True:
        return False, None
    data = result.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("order"), dict):
        return False, None
    status = data["order"].get("status")
    return isinstance(status, str), status if isinstance(status, str) else None


def _status_message(status: str) -> str:
    if status == ActionStatus.SUCCEEDED.value:
        return (
            "退款操作已完成，订单已关闭（CLOSE）。"
            "订单关闭不代表支付渠道资金已经到账。"
        )
    if status in {
        ActionStatus.CONFIRMED.value,
        ActionStatus.EXECUTING.value,
    }:
        return "退款操作正在处理中，请勿重复确认。"
    if status == ActionStatus.UNKNOWN.value:
        return "退款结果暂时无法确定，当前处于待对账状态，请稍后查询。"
    if status == ActionStatus.FAILED.value:
        return "退款操作未完成，请重新发起退款提议或联系人工客服。"
    return "退款提议仍在等待确认。"


def _transition(
    store: AgentActionStore,
    action: dict[str, Any],
    to_status: ActionStatus,
) -> dict[str, Any] | None:
    return store.transition_status(
        action_id=str(action["action_id"]),
        from_status=ActionStatus(str(action["status"])),
        to_status=to_status,
        expected_version=int(action["version"]),
    )


def _finish(
    store: AgentActionStore,
    action: dict[str, Any],
    status: ActionStatus,
    message: str,
) -> dict[str, object]:
    transitioned = _transition(store, action, status)
    latest = transitioned or store.get_action(str(action["action_id"]))
    final_status = (
        str(latest["status"])
        if latest is not None
        else ActionStatus.UNKNOWN.value
    )
    return {
        "final_status": final_status,
        "message": message if transitioned is not None else _status_message(final_status),
        "action_version": int(latest["version"]) if latest else 0,
    }


def _compile_confirmation_graph(checkpointer: object):
    def confirm_node(state: ConfirmationState) -> dict[str, object]:
        resume = interrupt(
            {
                "action_id": state["action_id"],
                "status": ActionStatus.PROPOSED.value,
            }
        )
        if not isinstance(resume, dict) or resume.get(
            "credential_validated"
        ) is not True:
            return {
                "confirmed": False,
                "execution_acquired": False,
                "error": "确认凭证未通过服务端校验",
            }

        store = AgentActionStore()
        action = store.get_action(state["action_id"])
        if action is None:
            return {
                "confirmed": False,
                "execution_acquired": False,
                "error": "退款提议不存在",
            }
        confirmed = store.confirm_refund_proposal(
            action_id=state["action_id"],
            user_id=str(action["user_id"]),
            expected_version=state["action_version"],
        )
        latest = store.get_action(state["action_id"])
        return {
            "confirmed": confirmed,
            "execution_acquired": False,
            "request_id": str(resume.get("request_id") or ""),
            "action_version": int(latest["version"]) if latest else 0,
            "final_status": str(latest["status"]) if latest else "",
            "error": "" if confirmed else "退款提议状态或版本已变化",
        }

    def execute_node(state: ConfirmationState) -> dict[str, object]:
        if state.get("confirmed") is not True:
            return {"execution_acquired": False}
        store = AgentActionStore()
        action = store.get_action(state["action_id"])
        if action is None or action.get("status") != ActionStatus.CONFIRMED.value:
            return {
                "execution_acquired": False,
                "final_status": str(action["status"]) if action else "",
                "error": "退款 action 已不处于 CONFIRMED 状态",
            }
        executing = _transition(store, action, ActionStatus.EXECUTING)
        if executing is None:
            latest = store.get_action(state["action_id"])
            return {
                "execution_acquired": False,
                "final_status": str(latest["status"]) if latest else "",
                "error": "退款执行权 CAS 失败",
            }

        result = execute_refund(
            executing,
            request_id=state.get("request_id", ""),
        )
        current = executing
        if result.certainty is ExecutionCertainty.UNKNOWN:
            unknown = _transition(store, executing, ActionStatus.UNKNOWN)
            if unknown is not None:
                current = unknown
        return {
            "execution_acquired": True,
            "execution_certainty": result.certainty.value,
            "result_code": result.result_code,
            "action_version": int(current["version"]),
            "final_status": str(current["status"]),
            "retry_count": 0,
        }

    def reconcile_node(state: ConfirmationState) -> dict[str, object]:
        store = AgentActionStore()
        action = store.get_action(state["action_id"])
        if action is None:
            return {
                "final_status": ActionStatus.UNKNOWN.value,
                "message": "退款 action 不存在，无法完成对账。",
            }
        if state.get("execution_acquired") is not True:
            status = str(action["status"])
            return {
                "final_status": status,
                "message": _status_message(status),
            }

        request_id = state.get("request_id", "")
        determined, order_status = _order_status(
            action,
            request_id=request_id,
        )
        if determined and order_status == "CLOSE":
            return _finish(
                store,
                action,
                ActionStatus.SUCCEEDED,
                _status_message(ActionStatus.SUCCEEDED.value),
            )

        certainty = ExecutionCertainty(
            state.get("execution_certainty", ExecutionCertainty.UNKNOWN.value)
        )
        result_code = state.get("result_code", "UNKNOWN_RESULT")
        if determined and certainty is ExecutionCertainty.FAILURE:
            message = (
                "订单或团队状态已经变化，旧退款提议未执行，"
                "请重新发起退款提议。"
                if result_code == "VERSION_CHANGED"
                else f"退款操作未完成：{result_code}。请重新发起退款提议。"
            )
            return _finish(
                store,
                action,
                ActionStatus.FAILED,
                message,
            )

        if action.get("status") == ActionStatus.EXECUTING.value:
            unknown = _transition(store, action, ActionStatus.UNKNOWN)
            if unknown is not None:
                action = unknown
        if action.get("status") != ActionStatus.UNKNOWN.value:
            latest_status = str(action["status"])
            return {
                "final_status": latest_status,
                "message": _status_message(latest_status),
            }

        retry_result: RefundExecutionResult = execute_refund(
            action,
            request_id=request_id,
        )
        retry_determined, retry_order_status = _order_status(
            action,
            request_id=request_id,
        )
        if retry_determined and retry_order_status == "CLOSE":
            result = _finish(
                store,
                action,
                ActionStatus.SUCCEEDED,
                _status_message(ActionStatus.SUCCEEDED.value),
            )
            result["retry_count"] = 1
            return result
        if (
            retry_determined
            and retry_result.certainty is ExecutionCertainty.FAILURE
        ):
            message = (
                "订单或团队状态已经变化，旧退款提议未执行，"
                "请重新发起退款提议。"
                if retry_result.result_code == "VERSION_CHANGED"
                else (
                    f"退款操作未完成：{retry_result.result_code}。"
                    "请重新发起退款提议。"
                )
            )
            result = _finish(
                store,
                action,
                ActionStatus.FAILED,
                message,
            )
            result["retry_count"] = 1
            return result
        return {
            "final_status": ActionStatus.UNKNOWN.value,
            "message": (
                "退款结果暂时无法确定；已使用原幂等键完成一次对账重试，"
                "当前保持 UNKNOWN，请稍后查询。"
            ),
            "retry_count": 1,
            "action_version": int(action["version"]),
        }

    graph = StateGraph(ConfirmationState)
    graph.add_node("confirm", confirm_node)
    graph.add_node("execute", execute_node)
    graph.add_node("reconcile", reconcile_node)
    graph.add_edge(START, "confirm")
    graph.add_edge("confirm", "execute")
    graph.add_edge("execute", "reconcile")
    graph.add_edge("reconcile", END)
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
