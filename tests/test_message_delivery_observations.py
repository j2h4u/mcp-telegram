"""Bounded diagnostics stay passive under loss and retain sampled identity."""

from collections.abc import Mapping
from typing import cast

from mcp_telegram.message_delivery_observations import MessageDeliveryObservations
from mcp_telegram.runtime_observations import encode_payload


class Sink:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.accept = True

    def record(self, *, kind: str, outcome: str | None = None, payload: Mapping[str, object] | None = None) -> bool:
        encode_payload(payload)
        self.rows.append({"kind": kind, "outcome": outcome, "payload": dict(payload or {})})
        return self.accept


def test_two_roots_per_window_and_eight_lifetime_stages() -> None:
    sink = Sink()
    observer = MessageDeliveryObservations(sink)
    first = observer.intake(source="push", scope="account", observed_at_ms=1)
    assert first is not None
    assert observer.intake(source="push", scope="account", observed_at_ms=2) is None
    second = observer.intake(source="rpc_response", scope="channel", observed_at_ms=3)
    assert second is not None
    for now, stage in enumerate(
        (
            "queued",
            "accepted",
            "admitted",
            "handler",
            "committed",
            "failed",
            "gap",
            "suppressed",
            "seq_duplicate",
            "pts_accepted",
            "pts_duplicate",
        ),
        4,
    ):
        observer.stage(first, scope="account", stage=stage, observed_at_ms=now)
    observer.flush()
    samples = [cast(dict[str, object], r["payload"]) for r in sink.rows if r["outcome"] == "sample"]
    assert len(samples) == 2
    assert len(first.stages) == 8
    assert first.truncated == 4
    assert samples[0]["trace_id"] == first.trace_id
    observer.stage(first, scope="account", stage="possible_gap", observed_at_ms=20)
    observer.flush()
    assert first.truncated == 5
    latest = cast(dict[str, object], [r for r in sink.rows if r["outcome"] == "sample"][-1]["payload"])
    assert latest["trace_id"] == first.trace_id


def test_rejected_sink_counts_loss_without_throwing() -> None:
    sink = Sink()
    sink.accept = False
    observer = MessageDeliveryObservations(sink)
    observer.intake(source="replay", scope="account", observed_at_ms=1)
    observer.flush()
    sink.accept = True
    observer.intake(source="push", scope="account", observed_at_ms=2)
    observer.flush()
    coverage = cast(dict[str, object], [r["payload"] for r in sink.rows if r["outcome"] == "coverage"][-1])
    assert cast(int, coverage["lost_rows"]) > 0


def test_sample_with_large_identities_and_counts_fits_sink_limit() -> None:
    sink = Sink()
    observer = MessageDeliveryObservations(sink)
    trace = observer.intake(source="push", scope="channel", observed_at_ms=1793800000000)
    assert trace is not None
    for number in range(4):
        observer.identity(trace, -9223372036854775807, 2147483647 - number, 1793800000000)
    for stage in (
        "queued",
        "accepted",
        "gap",
        "suppressed",
        "admitted",
        "handler",
        "committed",
        "failed",
        "pts_accepted",
        "pts_duplicate",
        "possible_gap",
        "getting_difference",
        "self_outgoing",
        "seq_duplicate",
        "channel_too_long",
        "catchup_selected",
        "catchup_blocked",
    ):
        observer.stage(trace, scope="channel", stage=stage, observed_at_ms=1793800000000)
    observer.flush()
    sample = cast(dict[str, object], next(r["payload"] for r in sink.rows if r["outcome"] == "sample"))
    assert len(encode_payload(sample).encode()) <= 1024
