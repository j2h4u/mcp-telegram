"""Authenticated acquisition scope for history and activity test ports."""

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.image_authorization import ScopeCell, ScopeToken


class HistoryScope:
    def __init__(self, account_id: int = 123) -> None:
        self.cell = ScopeCell()
        assert self.cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, account_id, 2, 99))

    def capture_scope(self) -> ScopeToken | None:
        return self.cell.capture()

    def claim_scope(self, token: ScopeToken) -> bool:
        return self.cell.claim(token)

    def capture_image_scope(self) -> ScopeToken | None:
        return self.cell.capture()

    def claim_image_scope(self, token: ScopeToken) -> bool:
        return self.cell.claim(token)
