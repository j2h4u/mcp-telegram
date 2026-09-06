"""Shared wire vocabulary for durable conversation changes."""

CHANGE_KINDS = ("edit", "deleted_message", "access_lost", "access_restored")

__all__ = ["CHANGE_KINDS"]
