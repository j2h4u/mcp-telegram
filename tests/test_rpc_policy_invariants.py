"""Production RPC budgets cannot be disabled through configuration or construction."""

import math
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram import telegram_rpc
from mcp_telegram.config import ConfigError, TelegramRpcConfig, load_config
from mcp_telegram.telegram_rpc import TelegramRpcBudget


@pytest.mark.parametrize("factory", [TelegramRpcBudget, TelegramRpcConfig])
@pytest.mark.parametrize("limit", [0, -1, True, False, 1.5, math.inf, "off", "unlimited", None])
def test_budget_limit_requires_positive_exact_integer(
    factory: type[TelegramRpcBudget] | type[TelegramRpcConfig], limit: object
) -> None:
    with pytest.raises(ValueError, match="max_calls_per_period"):
        factory(max_calls_per_period=cast(int, limit), period_seconds=60)


@pytest.mark.parametrize("factory", [TelegramRpcBudget, TelegramRpcConfig])
@pytest.mark.parametrize("period", [0, -1, True, False, math.inf, -math.inf, math.nan, "off", "unlimited", None])
def test_budget_period_requires_positive_finite_number(
    factory: type[TelegramRpcBudget] | type[TelegramRpcConfig], period: object
) -> None:
    with pytest.raises(ValueError, match="period_seconds"):
        factory(max_calls_per_period=30, period_seconds=cast(float, period))


@pytest.mark.parametrize("limit", ["0", "-1", "true", "false", "1.5", '"off"', '"unlimited"', "inf", "nan"])
def test_config_file_rejects_nonpositive_or_untyped_rpc_limit(tmp_path: Path, limit: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[state]\ndir="/state"\n[telegram_rpc]\ntransport_contract_version=1\nmax_calls_per_period=' + limit + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="max_calls_per_period"):
        load_config(path)


def test_default_budget_is_finite_and_has_no_disable_or_public_reset() -> None:
    config = TelegramRpcConfig()
    budget = TelegramRpcBudget(config.max_calls_per_period, config.period_seconds)
    assert (budget.max_calls_per_period, budget.period_seconds) == (30, 60)
    assert not hasattr(budget, "enabled")
    assert not hasattr(telegram_rpc, "reset_account_cooldown")
