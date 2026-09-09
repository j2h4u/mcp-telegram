from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest


def _gate() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "check_demand_cutover.py"
    spec = importlib.util.spec_from_file_location("check_demand_cutover", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def test_producer_offers_and_non_durable_runtime_tasks_are_allowed(tmp_path: Path) -> None:
    daemon = """
import asyncio

async def sync_main(ctx):
    ctx.coordinator.offer(DemandKind.SCHEDULED_REPAIR)
    _create_tracked_task(ctx, ctx.coordinator.run(), name="telegram_demand_coordinator")
    _create_tracked_task(ctx, run_reconnect_catch_up_loop(ctx), name="reconnect_catch_up_loop")
    _create_tracked_task(ctx, _monitor_flood_wait_kill_switch(ctx), name="flood_wait_kill_switch_monitor")
    _create_tracked_task(ctx, ctx.rpc_admission_observer.run_periodic_flush(ctx.shutdown_event),
                         name="rpc_admission_observation_flush_loop")
    await asyncio.sleep(0)
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


def test_current_pr1_checkout_is_explicitly_blocked_until_cutover() -> None:
    gate = _gate()
    findings = cast(tuple[object, ...], gate.find_violations(Path(__file__).parents[1]))

    assert findings
    assert any(getattr(finding, "rule", None) == "shadow-module" for finding in findings)
    assert any(getattr(finding, "rule", None) == "retired-durable-launch" for finding in findings)


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
