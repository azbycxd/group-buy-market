from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, AsyncIterator

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, Field

from agent import (
    DEFAULT_CHECKPOINT_PATH,
    close_async_order_agent,
    create_async_order_agent,
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
        queue: asyncio.Queue[dict[str, str]],
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
            _tool_progress(tool_name),
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


async def _run_agent(
    agent: Any,
    payload: ChatStreamRequest,
    queue: asyncio.Queue[dict[str, str]],
    user_id: str,
) -> AgentOutcome:
    loop = asyncio.get_running_loop()
    callback = ToolProgressCallback(loop, queue)
    config = {
        "callbacks": [callback],
        "configurable": {"thread_id": payload.session_id},
    }
    outcome: AgentOutcome | None = None

    async for update in agent.astream(
        {"messages": [HumanMessage(content=payload.message)]},
        context=AgentContext(user_id=user_id),
        config=config,
        stream_mode="updates",
    ):
        if not isinstance(update, dict):
            continue
        agent_update = update.get("agent")
        if callback.emitted == 0:
            for tool_name in _tool_names(agent_update):
                queue.put_nowait(_tool_progress(tool_name))
        finalize_update = update.get("finalize")
        if isinstance(finalize_update, dict) and finalize_update.get("outcome"):
            outcome = AgentOutcome.model_validate(finalize_update["outcome"])

    if outcome is None:
        snapshot = await agent.aget_state(config)
        outcome = AgentOutcome.model_validate(snapshot.values.get("outcome"))
    return outcome


def _record_execution(app: FastAPI, session_id: str, event: str) -> None:
    record = {
        "session_id": session_id,
        "event": event,
        "time": time.time(),
    }
    app.state.execution_events.append(record)
    logger.info("%s session_id=%s", event, session_id)


async def _cancel_task(
    app: FastAPI,
    session_id: str,
    task: asyncio.Task[AgentOutcome],
) -> None:
    if task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    _record_execution(app, session_id, "cancelled")


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
        app.state.agent = await create_async_order_agent(selected_checkpoint)
        try:
            app.state.session_ownership = await SessionOwnershipStore.open(
                selected_checkpoint
            )
        except Exception:
            await close_async_order_agent(app.state.agent)
            raise
        try:
            yield
        finally:
            await app.state.session_ownership.close()
            await close_async_order_agent(app.state.agent)

    app = FastAPI(title="Group Buy Agent API", lifespan=lifespan)

    @app.post("/api/v1/chat/stream")
    async def chat_stream(
        payload: ChatStreamRequest,
        request: Request,
        user_id: Annotated[str, Depends(authenticated_user_id)],
    ) -> StreamingResponse:
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
            queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
            execution: asyncio.Task[AgentOutcome] | None = None
            started_at = asyncio.get_running_loop().time()
            disconnected_recorded = False

            try:
                execution = asyncio.create_task(
                    _run_agent(app.state.agent, payload, queue, user_id)
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
                            "client_disconnected",
                        )
                        disconnected_recorded = True
                        await _cancel_task(app, payload.session_id, execution)
                        return

                    elapsed = asyncio.get_running_loop().time() - started_at
                    remaining = app.state.request_timeout_seconds - elapsed
                    if remaining <= 0:
                        await _cancel_task(app, payload.session_id, execution)
                        _record_execution(app, payload.session_id, "timeout")
                        yield _sse(
                            "timeout",
                            {
                                "kind": OutcomeKind.HANDOFF.value,
                                "answer": (
                                    "本次处理超时，请稍后重试或联系人工客服。"
                                ),
                            },
                        )
                        return

                    if not queue.empty():
                        yield _sse("progress", queue.get_nowait())
                        continue

                    if execution.done():
                        outcome = execution.result()
                        while not queue.empty():
                            yield _sse("progress", queue.get_nowait())
                        yield _sse(
                            "final",
                            {
                                "kind": outcome.kind.value,
                                "answer": outcome.final_answer,
                            },
                        )
                        return

                    try:
                        progress = await asyncio.wait_for(
                            queue.get(),
                            timeout=min(DISCONNECT_POLL_SECONDS, remaining),
                        )
                    except TimeoutError:
                        continue
                    yield _sse("progress", progress)
            except asyncio.CancelledError:
                if not disconnected_recorded:
                    _record_execution(
                        app,
                        payload.session_id,
                        "client_disconnected",
                    )
                if execution is not None:
                    await _cancel_task(app, payload.session_id, execution)
                raise
            except Exception:
                logger.exception(
                    "agent_stream_failed session_id=%s",
                    payload.session_id,
                )
                if execution is not None:
                    await _cancel_task(app, payload.session_id, execution)
                yield _sse(
                    "final",
                    {
                        "kind": OutcomeKind.HANDOFF.value,
                        "answer": "本次处理失败，已转人工客服核实。",
                    },
                )
            finally:
                if execution is not None and not execution.done():
                    await _cancel_task(app, payload.session_id, execution)
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

    return app


app = create_app()
