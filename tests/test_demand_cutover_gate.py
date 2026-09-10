from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Protocol, cast

import pytest


class _Finding(Protocol):
    def render(self) -> str: ...


class _DemandCutoverGate(Protocol):
    def find_violations(self, root: Path) -> tuple[_Finding, ...]: ...


def _gate() -> _DemandCutoverGate:
    path = Path(__file__).parents[1] / "scripts" / "check_demand_cutover.py"
    spec = importlib.util.spec_from_file_location("check_demand_cutover", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(_DemandCutoverGate, module)


def _fixture_root(
    tmp_path: Path,
    *,
    daemon: str = (
        "async def sync_main(ctx):\n"
        '    _create_tracked_task(ctx, ctx.coordinator.run(), name="telegram_demand_coordinator")\n'
    ),
) -> Path:
    source = tmp_path / "src" / "mcp_telegram"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "daemon.py").write_text(daemon, encoding="utf-8")
    return tmp_path


def _messages(root: Path) -> list[str]:
    gate = _gate()
    return [finding.render() for finding in gate.find_violations(root)]


def test_shadow_modules_symbols_and_fields_are_release_blockers(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    source = root / "src" / "mcp_telegram"
    (source / "demand_shadow_wiring.py").write_text("class DemandShadow: pass\n", encoding="utf-8")
    (source / "worker.py").write_text(
        "from .demand_shadow_wiring import DemandCycleRunner\n"
        "def use(TelegramDemandShadow):\n"
        "    demand_shadow = TelegramDemandShadow\n"
        "    return demand_shadow\n",
        encoding="utf-8",
    )

    findings = _messages(root)

    assert any("shadow-module" in message for message in findings)
    assert any("DemandCycleRunner" in message for message in findings)
    assert any("TelegramDemandShadow" in message for message in findings)
    assert any("shadow-field" in message for message in findings)


def test_shadow_declarations_and_shadow_named_modules_are_release_blockers(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    source = root / "src" / "mcp_telegram"
    (source / "telegram_demand_shadow_runtime.py").write_text("class Runtime: pass\n", encoding="utf-8")
    (source / "bindings.py").write_text(
        "class DemandShadowBridge: pass\ndef bind_demand_shadow(value):\n    return value\n",
        encoding="utf-8",
    )

    findings = _messages(root)

    assert any("shadow-module" in message for message in findings)
    assert any("DemandShadowBridge" in message for message in findings)
    assert any("bind_demand_shadow" in message for message in findings)


def test_daemon_rejects_legacy_loops_and_direct_worker_execution(tmp_path: Path) -> None:
    daemon = """
import asyncio

async def sync_main(ctx):
    _create_tracked_task(ctx, run_activity_sync_loop(ctx), name="activity_sync_loop")
    _create_tracked_task(ctx, ctx.folder_projection_worker.run(), name="folder_projection_worker")
    await _run_sync_loop(ctx)
"""
    messages = _messages(_fixture_root(tmp_path, daemon=daemon))

    assert any("run_activity_sync_loop" in message for message in messages)
    assert any("activity_sync_loop" in message for message in messages)
    assert any("directly invokes durable worker operation 'run'" in message for message in messages)
    assert any("run_sync_loop" in message for message in messages)


def test_daemon_rejects_aliased_legacy_loops_and_worker_methods(tmp_path: Path) -> None:
    daemon = """
from package import run_activity_sync_loop as legacy_loop

async def sync_main(ctx):
    launcher = legacy_loop
    worker_operation = ctx.folder_projection_worker.run
    _create_tracked_task(ctx, launcher(ctx), name="legacy")
    worker_operation()
    _create_tracked_task(ctx, ctx.coordinator.run(), name="telegram_demand_coordinator")
"""
    messages = _messages(_fixture_root(tmp_path, daemon=daemon))

    assert any("retired-durable-launch" in message for message in messages)
    assert any("direct-durable-operation" in message for message in messages)


def test_coordinator_alias_and_task_label_alias_are_counted(tmp_path: Path) -> None:
    daemon = """
async def sync_main(ctx):
    coordinator_run = ctx.coordinator.run
    coordinator_name = "telegram_demand_coordinator"
    _create_tracked_task(ctx, coordinator_run(), name=coordinator_name)
"""

    assert _messages(_fixture_root(tmp_path, daemon=daemon)) == []


def test_producer_offers_and_non_durable_runtime_tasks_are_allowed(tmp_path: Path) -> None:
    daemon = """
import asyncio

async def sync_main(ctx):
    ctx.coordinator.offer(DemandKind.SCHEDULED_REPAIR)
    _create_tracked_task(ctx, ctx.coordinator.run(), name="telegram_demand_coordinator")
    _create_tracked_task(ctx, run_reconnect_catch_up_loop(ctx), name="reconnect_catch_up_loop")
    _create_tracked_task(ctx, _monitor_flood_wait_kill_switch(ctx), name="flood_wait_kill_switch_monitor")
    await asyncio.sleep(0)
"""

    assert _messages(_fixture_root(tmp_path, daemon=daemon)) == []


def test_demand_wiring_offer_helper_is_allowed(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    (root / "src" / "mcp_telegram" / "producer.py").write_text(
        "from .demand_wiring import offer_durable_demand\n"
        "def emit(sink, kind):\n"
        "    offer_durable_demand(sink, kind)\n",
        encoding="utf-8",
    )

    assert _messages(root) == []


def test_inline_protocol_realtime_and_transport_tasks_are_allowed(tmp_path: Path) -> None:
    daemon = """
async def sync_main(ctx):
    ctx.coordinator.offer(DemandKind.SCHEDULED_REPAIR)
    _create_tracked_task(ctx, ctx.coordinator.run(), name="telegram_demand_coordinator")
    _create_tracked_task(ctx, ctx.inline.run(), name="inline")
    _create_tracked_task(ctx, ctx.protocol.run(), name="protocol")
    _create_tracked_task(ctx, ctx.realtime.run(), name="realtime")
    _create_tracked_task(ctx, ctx.transport.run(), name="transport")
"""

    assert _messages(_fixture_root(tmp_path, daemon=daemon)) == []


def test_more_than_one_coordinator_task_is_rejected(tmp_path: Path) -> None:
    daemon = """
async def sync_main(ctx):
    _create_tracked_task(ctx, ctx.coordinator.run(), name="telegram_demand_coordinator")
    _create_tracked_task(ctx, ctx.coordinator.run(), name="telegram_demand_coordinator_retry")
"""
    messages = _messages(_fixture_root(tmp_path, daemon=daemon))

    assert any("expected one coordinator task launch, found 2" in message for message in messages)


def test_current_checkout_has_completed_the_cutover() -> None:
    gate = _gate()
    findings = gate.find_violations(Path(__file__).parents[1])

    assert findings == ()


@pytest.mark.parametrize(
    "source",
    [
        "# DemandShadow is historical documentation\n",
        'label = "telegram_demand_shadow is a retired name"\n',
    ],
)
def test_comments_and_unrelated_strings_do_not_trigger_shadow_findings(tmp_path: Path, source: str) -> None:
    root = _fixture_root(tmp_path)
    (root / "src" / "mcp_telegram" / "doc.py").write_text(source, encoding="utf-8")

    messages = _messages(root)

    assert messages == []


def test_ast_fixture_is_valid_before_running_structural_gate() -> None:
    ast.parse("async def run():\n    return None\n")
