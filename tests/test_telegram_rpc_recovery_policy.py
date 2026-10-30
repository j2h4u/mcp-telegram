import pytest
from telethon.tl import functions
from telethon.tl.alltlobjects import tlobjects

from mcp_telegram import telegram_rpc_consumers as consumers
from mcp_telegram.telegram_rpc_consumers import RpcEffect, rpc_effect
from mcp_telegram.telegram_rpc_recovery import RecoveryCause, backoff_seconds


def canonical(cls: type) -> str:
    return f"{cls.__module__}.{cls.__name__}:{cls.CONSTRUCTOR_ID:08x}"


def test_reviewed_constructor_ids_match_pinned_telethon() -> None:
    for effect, methods in (
        (RpcEffect.NONMUTATING_READ, consumers._READ_METHODS),
        (RpcEffect.MUTATION, consumers._MUTATION_METHODS),
        (RpcEffect.AUTH_TRANSITION, consumers._AUTH_METHODS),
    ):
        for method in methods:
            cls = tlobjects[int(method.rsplit(":", 1)[1], 16)]
            assert canonical(cls) == method
            assert rpc_effect(method) is effect


def test_exact_classification_fails_closed() -> None:
    assert rpc_effect(canonical(functions.users.GetUsersRequest)) is RpcEffect.NONMUTATING_READ
    assert rpc_effect(canonical(functions.messages.GetHistoryRequest)) is RpcEffect.NONMUTATING_READ
    assert rpc_effect(canonical(functions.updates.GetDifferenceRequest)) is RpcEffect.NONMUTATING_READ
    assert rpc_effect(canonical(functions.messages.SaveDraftRequest)) is RpcEffect.MUTATION
    assert rpc_effect(canonical(functions.auth.LogOutRequest)) is RpcEffect.AUTH_TRANSITION
    assert rpc_effect("users.GetUsersRequest") is RpcEffect.UNCLASSIFIED
    assert rpc_effect("telethon.tl.functions.users.GetUsersRequest:00000000") is RpcEffect.UNCLASSIFIED
    assert rpc_effect("telethon.tl.functions.messages.GetFakeRequest:0d91a548") is RpcEffect.UNCLASSIFIED


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (RecoveryCause.FLOOD_WAIT, [3600, 7200, 14400, 28800, 57600, 86400, 86400]),
        (RecoveryCause.TRANSIENT, [30, 60, 120, 240, 480, 960, 1800]),
        (RecoveryCause.READ_UNCERTAINTY, [300, 600, 1200, 2400, 3600, 3600, 3600]),
    ],
)
def test_backoff_policies(cause: RecoveryCause, expected: list[int]) -> None:
    assert [backoff_seconds(cause, stage) for stage in range(7)] == expected
    assert backoff_seconds(cause, 10**9) == expected[-1]
    with pytest.raises(ValueError):
        backoff_seconds(cause, -1)
