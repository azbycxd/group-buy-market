from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, AsyncIterator

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from action_ledger import (
    ActionStatus,
    AgentActionStore,
    action_args_hash_is_current,
    confirmation_credential,
    credential_matches,
    required_confirmation_secret,
)
from agent import (
    DEFAULT_CHECKPOINT_PATH,
    close_async_order_agent,
    create_async_order_agent,
)
from confirmation_workflow import (
    close_async_confirmation_workflow,
    confirmation_config,
    create_async_confirmation_workflow,
)
from outcome import AgentOutcome, OutcomeKind
from session_locks import SessionLockRegistry
from session_ownership import SessionOwnershipStore
from tools.context import AgentContext


logger = logging.getLogger("uvicorn.error")
DEFAULT_REQUEST_TIMEOUT_SECONDS = 25.0
DISCONNECT_POLL_SECONDS = 0.1
JWT_ALGORITHM = "HS256"
bearer_scheme = HTTPBearer(auto_error=False)

TOOL_PROGRESS_MESSAGES = {
    "get_order_facts": "正在查询订单事实",
    "get_activity_facts": "正在查询活动事实",
    "get_user_eligibility_facts": "正在查询参与资格",
    "get_joinable_team_facts": "正在查询可加入团队",
    "search_group_buy_rules": "正在查询拼团规则",
}


class ChatStreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ActionConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: str = Field(min_length=64, max_length=64)


def _positive_float(value: str | None, default: float) -> float:
    try:
        parsed = float(value) if value is not None else default
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _required_jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError("缺少环境变量: JWT_SECRET")
    return secret


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效或已过期的访问令牌",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _confirmation_error(
    status_code: int,
    code: str,
    message: str,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validated_confirmation_identity(
    store: AgentActionStore,
    *,
    action_id: str,
    user_id: str,
    credential: str,
) -> dict[str, Any]:
    action = store.get_action(action_id)
    if action is None:
        raise _confirmation_error(
            status.HTTP_404_NOT_FOUND,
            "ACTION_NOT_FOUND",
            "退款提议不存在",
        )
    if action.get("user_id") != user_id:
        raise _confirmation_error(
            status.HTTP_403_FORBIDDEN,
            "ACTION_FORBIDDEN",
            "无权确认该退款提议",
        )
    if not action_args_hash_is_current(action):
        raise _confirmation_error(
            status.HTTP_409_CONFLICT,
            "ACTION_ARGS_CHANGED",
            "退款提议参数已变化，请重新发起",
        )
    if not credential_matches(
        action,
        credential,
        secret=required_confirmation_secret(),
    ):
        raise _confirmation_error(
            status.HTTP_403_FORBIDDEN,
            "INVALID_CONFIRMATION_CREDENTIAL",
            "确认凭证无效",
        )
    return action


def _validated_confirmation_action(
    store: AgentActionStore,
    *,
    action_id: str,
    user_id: str,
    credential: str,
) -> dict[str, Any]:
    action = _validated_confirmation_identity(
        store,
        action_id=action_id,
        user_id=user_id,
        credential=credential,
    )
    if action.get("status") != ActionStatus.PROPOSED.value:
        raise _confirmation_error(
            status.HTTP_409_CONFLICT,
            "ACTION_NOT_PROPOSED",
            "退款提议已不处于待确认状态",
        )
    expires_at = _parse_timestamp(action.get("expires_at"))
    if expires_at is None or expires_at <= datetime.now(timezone.utc):
        raise _confirmation_error(
            status.HTTP_410_GONE,
            "ACTION_EXPIRED",
            "退款提议已过期，请重新发起",
        )
    return action


def _action_status_response(
    action: dict[str, Any],
    *,
    message: str | None = None,
) -> JSONResponse:
    action_status = str(action["status"])
    if action_status == ActionStatus.SUCCEEDED.value:
        status_code = status.HTTP_200_OK
        default_message = (
            "退款操作已完成，订单已关闭（CLOSE）。"
            "订单关闭不代表支付渠道资金已经到账。"
        )
    elif action_status in {
        ActionStatus.CONFIRMED.value,
        ActionStatus.EXECUTING.value,
        ActionStatus.UNKNOWN.value,
    }:
        status_code = status.HTTP_202_ACCEPTED
        default_message = (
            "退款操作正在处理中。"
            if action_status != ActionStatus.UNKNOWN.value
            else "退款结果暂时无法确定，当前处于待对账状态。"
        )
    elif action_status == ActionStatus.FAILED.value:
        status_code = status.HTTP_409_CONFLICT
        default_message = "退款操作未完成，请重新发起退款提议。"
    else:
        status_code = status.HTTP_202_ACCEPTED
        default_message = "退款确认正在处理中。"
    return JSONResponse(
        status_code=status_code,
        content={
            "action_id": str(action["action_id"]),
            "status": action_status,
            "version": int(action["version"]),
            "executed": action_status == ActionStatus.SUCCEEDED.value,
            "message": message or default_message,
        },
    )


async def authenticated_user_id(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        claims = jwt.decode(
            credentials.credentials,
            request.app.state.jwt_secret,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "exp"]},
        )
    except jwt.PyJWTError as error:
        raise _unauthorized() from error
    user_id = claims.get("sub")
    if not isinstance(user_id, str) or not user_id.strip():
        raise _unauthorized()
    return user_id


def _sse(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def _tool_progress(tool_name: str) -> dict[str, str]:
    return {
        "stage": "tool",
        "tool": tool_name,
        "message": TOOL_PROGRESS_MESSAGES.get(tool_name, "正在查询业务事实"),
    }


class ToolProgressCallback(BaseCallbackHandler):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    ) -> None:
        self._loop = loop
        self._queue = queue
        self._lock = Lock()
        self._emitted = 0

    @property
    def emitted(self) -> int:
        with self._lock:
            return self._emitted

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        del input_str, kwargs
        tool_name = str(serialized.get("name") or "unknown_tool")
        with self._lock:
            self._emitted += 1
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait,
            ("progress", _tool_progress(tool_name)),
        )


def _tool_names(update: Any) -> list[str]:
    if not isinstance(update, dict):
        return []
    names: list[str] = []
    for message in update.get("messages", []):
        if not isinstance(message, AIMessage):
            continue
        names.extend(call["name"] for call in message.tool_calls)
    return names


def _action_proposed_payload(
    store: AgentActionStore,
    *,
    action_id: str,
    session_id: str,
    user_id: str,
) -> dict[str, Any] | None:
    action = store.get_action(action_id)
    if (
        action is None
        or action.get("session_id") != session_id
        or action.get("user_id") != user_id
        or action.get("status") != "PROPOSED"
        or not action_args_hash_is_current(action)
    ):
        return None
    try:
        preview = json.loads(str(action["preview_json"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(preview, dict):
        return None
    return {
        "action_id": action_id,
        "credential": confirmation_credential(
            action,
            secret=required_confirmation_secret(),
        ),
        "expires_at": action["expires_at"],
        "preview": preview,
    }


async def _run_agent(
    agent: Any,
    payload: ChatStreamRequest,
    queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    action_store: AgentActionStore,
    user_id: str,
    request_id: str,
) -> AgentOutcome:
    loop = asyncio.get_running_loop()
    callback = ToolProgressCallback(loop, queue)
    config = {
        "callbacks": [callback],
        "configurable": {"thread_id": payload.session_id},
        "metadata": {
            "request_id": request_id,
            "session_id": payload.session_id,
        },
        "run_name": "group-buy-agent-request",
    }
    outcome: AgentOutcome | None = None

    async for update in agent.astream(
        {"messages": [HumanMessage(content=payload.message)]},
        context=AgentContext(user_id=user_id, request_id=request_id),
        config=config,
        stream_mode="updates",
    ):
        if not isinstance(update, dict):
            continue
        agent_update = update.get("agent")
        if callback.emitted == 0:
            for tool_name in _tool_names(agent_update):
                queue.put_nowait(("progress", _tool_progress(tool_name)))
        for node_name in ("finalize", "propose_refund"):
            node_update = update.get(node_name)
            if isinstance(node_update, dict) and node_update.get("outcome"):
                outcome = AgentOutcome.model_validate(node_update["outcome"])
            if node_name != "propose_refund" or not isinstance(
                node_update,
                dict,
            ):
                continue
            action_id = node_update.get("proposed_action_id")
            if not isinstance(action_id, str) or not action_id:
                continue
            action_event = _action_proposed_payload(
                action_store,
                action_id=action_id,
                session_id=payload.session_id,
                user_id=user_id,
            )
            if action_event is not None:
                queue.put_nowait(("action_proposed", action_event))

    if outcome is None:
        snapshot = await agent.aget_state(config)
        outcome = AgentOutcome.model_validate(snapshot.values.get("outcome"))
    return outcome


def _record_execution(
    app: FastAPI,
    session_id: str,
    request_id: str,
    event: str,
) -> None:
    record = {
        "session_id": session_id,
        "request_id": request_id,
        "event": event,
        "time": time.time(),
    }
    app.state.execution_events.append(record)
    logger.info(
        "%s session_id=%s request_id=%s",
        event,
        session_id,
        request_id,
    )


async def _cancel_task(
    app: FastAPI,
    session_id: str,
    request_id: str,
    task: asyncio.Task[AgentOutcome],
) -> None:
    if task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    _record_execution(app, session_id, request_id, "cancelled")


def create_app(
    *,
    checkpoint_path: str | Path | None = None,
    request_timeout_seconds: float | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        selected_checkpoint = checkpoint_path or Path(
            os.getenv("CHECKPOINT_DB_PATH", str(DEFAULT_CHECKPOINT_PATH))
        )
        app.state.jwt_secret = _required_jwt_secret()
        app.state.request_timeout_seconds = (
            request_timeout_seconds
            if request_timeout_seconds is not None
            else _positive_float(
                os.getenv("AGENT_REQUEST_TIMEOUT_SECONDS"),
                DEFAULT_REQUEST_TIMEOUT_SECONDS,
            )
        )
        app.state.execution_events = []
        app.state.session_locks = SessionLockRegistry()
        app.state.confirmation_locks = SessionLockRegistry()
        app.state.action_store = AgentActionStore()
        app.state.agent = await create_async_order_agent(selected_checkpoint)
        try:
            app.state.confirmation_graph = (
                await create_async_confirmation_workflow(
                    selected_checkpoint
                )
            )
            try:
                app.state.session_ownership = await SessionOwnershipStore.open(
                    selected_checkpoint
                )
                try:
                    yield
                finally:
                    await app.state.session_ownership.close()
            finally:
                await close_async_confirmation_workflow(
                    app.state.confirmation_graph
                )
        finally:
            await close_async_order_agent(app.state.agent)

    app = FastAPI(title="Group Buy Agent API", lifespan=lifespan)

    @app.post("/api/v1/chat/stream")
    async def chat_stream(
        payload: ChatStreamRequest,
        request: Request,
        user_id: Annotated[str, Depends(authenticated_user_id)],
    ) -> StreamingResponse:
        request_id = uuid.uuid4().hex
        if not await app.state.session_ownership.claim(
            payload.session_id,
            user_id,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="该 session_id 已属于其他用户",
            )
        session_lock = await app.state.session_locks.try_acquire(
            payload.session_id
        )
        if session_lock is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "SESSION_BUSY",
                    "message": "该会话正在处理另一个请求，请稍后重试。",
                },
            )

        async def event_stream() -> AsyncIterator[bytes]:
            queue: asyncio.Queue[
                tuple[str, dict[str, Any]]
            ] = asyncio.Queue()
            execution: asyncio.Task[AgentOutcome] | None = None
            started_at = asyncio.get_running_loop().time()
            disconnected_recorded = False

            try:
                _record_execution(
                    app,
                    payload.session_id,
                    request_id,
                    "request_started",
                )
                execution = asyncio.create_task(
                    _run_agent(
                        app.state.agent,
                        payload,
                        queue,
                        app.state.action_store,
                        user_id,
                        request_id,
                    )
                )
                yield _sse(
                    "progress",
                    {
                        "stage": "understanding",
                        "message": "正在理解问题",
                    },
                )

                while True:
                    if await request.is_disconnected():
                        _record_execution(
                            app,
                            payload.session_id,
                            request_id,
                            "client_disconnected",
                        )
                        disconnected_recorded = True
                        await _cancel_task(
                            app,
                            payload.session_id,
                            request_id,
                            execution,
                        )
                        return

                    elapsed = asyncio.get_running_loop().time() - started_at
                    remaining = app.state.request_timeout_seconds - elapsed
                    if remaining <= 0:
                        await _cancel_task(
                            app,
                            payload.session_id,
                            request_id,
                            execution,
                        )
                        _record_execution(
                            app,
                            payload.session_id,
                            request_id,
                            "timeout",
                        )
                        yield _sse(
                            "timeout",
                            {
                                "kind": OutcomeKind.HANDOFF.value,
                                "answer": (
                                    "本次处理超时，请稍后重试或联系人工客服。"
                                ),
                                "request_id": request_id,
                            },
                        )
                        return

                    if not queue.empty():
                        event_name, event_data = queue.get_nowait()
                        yield _sse(event_name, event_data)
                        continue

                    if execution.done():
                        outcome = execution.result()
                        while not queue.empty():
                            event_name, event_data = queue.get_nowait()
                            yield _sse(event_name, event_data)
                        _record_execution(
                            app,
                            payload.session_id,
                            request_id,
                            "request_completed",
                        )
                        yield _sse(
                            "final",
                            {
                                "kind": outcome.kind.value,
                                "answer": outcome.final_answer,
                                "request_id": request_id,
                            },
                        )
                        return

                    try:
                        event_name, event_data = await asyncio.wait_for(
                            queue.get(),
                            timeout=min(DISCONNECT_POLL_SECONDS, remaining),
                        )
                    except TimeoutError:
                        continue
                    yield _sse(event_name, event_data)
            except asyncio.CancelledError:
                if not disconnected_recorded:
                    _record_execution(
                        app,
                        payload.session_id,
                        request_id,
                        "client_disconnected",
                    )
                if execution is not None:
                    await _cancel_task(
                        app,
                        payload.session_id,
                        request_id,
                        execution,
                    )
                raise
            except Exception:
                logger.exception(
                    "agent_stream_failed session_id=%s",
                    payload.session_id,
                )
                if execution is not None:
                    await _cancel_task(
                        app,
                        payload.session_id,
                        request_id,
                        execution,
                    )
                yield _sse(
                    "final",
                    {
                        "kind": OutcomeKind.HANDOFF.value,
                        "answer": "本次处理失败，已转人工客服核实。",
                        "request_id": request_id,
                    },
                )
            finally:
                if execution is not None and not execution.done():
                    await _cancel_task(
                        app,
                        payload.session_id,
                        request_id,
                        execution,
                    )
                app.state.session_locks.release(
                    payload.session_id,
                    session_lock,
                )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/v1/actions/{action_id}/confirm")
    async def confirm_action(
        action_id: str,
        payload: ActionConfirmRequest,
        user_id: Annotated[str, Depends(authenticated_user_id)],
    ) -> JSONResponse:
        action = _validated_confirmation_identity(
            app.state.action_store,
            action_id=action_id,
            user_id=user_id,
            credential=payload.credential,
        )
        if action.get("status") != ActionStatus.PROPOSED.value:
            return _action_status_response(action)
        _validated_confirmation_action(
            app.state.action_store,
            action_id=action_id,
            user_id=user_id,
            credential=payload.credential,
        )
        action_lock = await app.state.confirmation_locks.try_acquire(
            action_id
        )
        if action_lock is None:
            current = _validated_confirmation_identity(
                app.state.action_store,
                action_id=action_id,
                user_id=user_id,
                credential=payload.credential,
            )
            return _action_status_response(current)
        try:
            action = _validated_confirmation_action(
                app.state.action_store,
                action_id=action_id,
                user_id=user_id,
                credential=payload.credential,
            )
            try:
                result = await app.state.confirmation_graph.ainvoke(
                    Command(
                        resume={
                            "credential_validated": True,
                            "request_id": uuid.uuid4().hex,
                        }
                    ),
                    config=confirmation_config(action_id),
                )
            except Exception as error:
                logger.warning(
                    "confirmation_resume_failed action_id=%s error=%s",
                    action_id,
                    type(error).__name__,
                )
                current = app.state.action_store.get_action(action_id)
                if current is not None and current.get("status") in {
                    ActionStatus.EXECUTING.value,
                    ActionStatus.UNKNOWN.value,
                }:
                    return _action_status_response(
                        current,
                        message=(
                            "退款结果暂时无法确认，正在等待对账。"
                        ),
                    )
                if current is not None and current.get("status") in {
                    ActionStatus.SUCCEEDED.value,
                    ActionStatus.FAILED.value,
                }:
                    return _action_status_response(current)
                raise _confirmation_error(
                    status.HTTP_409_CONFLICT,
                    "CONFIRMATION_WORKFLOW_UNAVAILABLE",
                    "确认工作流不可恢复，请重新发起退款提议",
                ) from error
            current = app.state.action_store.get_action(action_id)
            if current is None:
                raise _confirmation_error(
                    status.HTTP_404_NOT_FOUND,
                    "ACTION_NOT_FOUND",
                    "退款 action 不存在",
                )
            return _action_status_response(
                current,
                message=(
                    str(result["message"])
                    if result.get("message")
                    else None
                ),
            )
        finally:
            app.state.confirmation_locks.release(
                action_id,
                action_lock,
            )

    return app


app = create_app()
