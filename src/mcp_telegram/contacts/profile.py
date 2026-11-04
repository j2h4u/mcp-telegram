"""Separate account-saved contact values from public profile identity."""

from dataclasses import replace

from ..entity_profile.contracts import (
    ProfileFieldClass,
    ProfileFieldCoverage,
    ProfileFieldState,
    ProfileSemanticSnapshot,
)


def project_contact_profile(snapshot: ProfileSemanticSnapshot, user: object) -> ProfileSemanticSnapshot:
    contact = snapshot.coverage.get("user.contact")
    member = snapshot.fields.get("user.contact")
    if getattr(user, "min", False) or contact is None or not contact.authoritative or not isinstance(member, bool):
        return snapshot
    fields = dict(snapshot.fields)
    coverage = dict(snapshot.coverage)
    evidence = ProfileFieldCoverage(ProfileFieldClass.PRIVATE_FACT, ProfileFieldState.PRESENT, True)
    if member:
        fields["owner.saved_contact_name"] = {
            "first_name": getattr(user, "first_name", None) or "",
            "last_name": getattr(user, "last_name", None) or "",
        }
        phone = getattr(user, "phone", None)
        fields["owner.saved_contact_phone_access"] = bool(phone)
        for path in ("user.first_name", "user.last_name"):
            fields.pop(path, None)
            coverage.pop(path, None)
        if not phone:
            fields.pop("user.phone", None)
            coverage.pop("user.phone", None)
    else:
        fields["owner.saved_contact_name"] = {"first_name": None, "last_name": None}
        fields["owner.saved_contact_phone_access"] = None
    coverage["owner.saved_contact_name"] = evidence
    coverage["owner.saved_contact_phone_access"] = evidence
    return replace(snapshot, fields=fields, coverage=coverage)
