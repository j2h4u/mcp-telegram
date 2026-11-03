"""Receipt identities distinguish domains and cannot change in flight."""

from dataclasses import FrozenInstanceError, replace

import pytest

from mcp_telegram.native_write_fence import NativeWriteReceiptBinding


def test_binding_is_immutable_and_domain_scoped() -> None:
    binding = NativeWriteReceiptBinding("folder", 42, 2, 123, "key", "update", 8, "fingerprint", 1)
    assert replace(binding, domain="contact_note") != binding
    with pytest.raises(FrozenInstanceError):
        binding.target = 9  # type: ignore[misc]
    with pytest.raises(ValueError):
        replace(binding, binding_revision=0)


@pytest.mark.parametrize(
    "field,value",
    [("domain", "other"), ("account_id", True), ("target", 0), ("operation_key", "")],
)
def test_invalid_binding_cannot_identify_a_receipt(field: str, value: object) -> None:
    binding = NativeWriteReceiptBinding("folder", 42, 2, 123, "key", "update", 8, "fingerprint", 1)
    with pytest.raises(ValueError):
        replace(binding, **{field: value})
