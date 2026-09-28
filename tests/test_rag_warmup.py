from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import api
import rag.pipeline as pipeline_module


def test_explicit_warmup_reuses_process_singleton(monkeypatch: object) -> None:
    created: list[object] = []

    class FakePipeline:
        def __init__(self) -> None:
            created.append(self)

    monkeypatch.setattr(pipeline_module, "_PIPELINE", None)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        pipeline_module,
        "RuleSearchPipeline",
        FakePipeline,
    )

    warmed = pipeline_module.warmup_rule_search_pipeline()
    requested = pipeline_module.get_rule_search_pipeline()

    assert warmed is requested
    assert created == [warmed]


def test_api_warmup_waits_and_logs_only_completion_fields(
    monkeypatch: object,
    caplog: object,
) -> None:
    calls: list[str] = []

    def fake_warmup() -> object:
        calls.append("warmup")
        return object()

    monkeypatch.setattr(  # type: ignore[attr-defined]
        api,
        "warmup_rule_search_pipeline",
        fake_warmup,
    )
    app = SimpleNamespace(state=SimpleNamespace())
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):  # type: ignore[attr-defined]
        asyncio.run(api._warmup_rag(app))

    assert calls == ["warmup"]
    assert app.state.rag_warmup_latency_ms >= 0
    warmup_messages = [
        record.getMessage()  # type: ignore[attr-defined]
        for record in caplog.records  # type: ignore[attr-defined]
        if "rag_warmup_completed" in record.getMessage()  # type: ignore[attr-defined]
    ]
    assert len(warmup_messages) == 1
    assert warmup_messages[0].startswith(
        "rag_warmup_completed latency_ms="
    )
