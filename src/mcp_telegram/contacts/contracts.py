"""Account-owned contact observations and local projections."""

from dataclasses import dataclass
from typing import Literal, TypedDict


CONTACT_PAGE_SIZE_LIMIT = 100


class ContactCommandState(TypedDict):
    membership: bool | None
    first_name: str | None
    last_name: str | None
    phone_state: Literal["available", "unavailable", "unknown"]
    phone: str | None
    expected_version: str | None
    observation_order: int | None


@dataclass(frozen=True, slots=True)
class ContactProfileAcquisition:
    accepted: bool
    observation_order: int | None = None


@dataclass(frozen=True, slots=True)
class ContactCatalogueRefreshResult:
    accepted: bool
    observation_order: int | None = None
