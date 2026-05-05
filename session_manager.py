"""User session state management for interactive menus."""

import time
from typing import Any


class SessionManager:
    """Manage user session states with automatic expiration."""

    def __init__(self, timeout_seconds: int = 300):
        """Initialize session manager.

        Args:
            timeout_seconds: Session expiration time in seconds (default 5 minutes)
        """
        self._sessions: dict[int, dict[str, Any]] = {}
        self._timeout = timeout_seconds

    def set_state(self, user_id: int, state: str, **kwargs) -> None:
        """Set user session state with additional context.

        Args:
            user_id: QQ user ID
            state: State identifier (e.g., "awaiting_keyword", "menu_status")
            **kwargs: Additional context data (e.g., group_id, action)
        """
        self._sessions[user_id] = {
            "state": state,
            "expire": time.time() + self._timeout,
            **kwargs
        }

    def get_state(self, user_id: int) -> dict[str, Any] | None:
        """Get user session state if not expired.

        Args:
            user_id: QQ user ID

        Returns:
            Session dict or None if expired/not found
        """
        session = self._sessions.get(user_id)
        if not session:
            return None

        if time.time() > session["expire"]:
            self.clear_state(user_id)
            return None

        return session

    def clear_state(self, user_id: int) -> None:
        """Clear user session state.

        Args:
            user_id: QQ user ID
        """
        self._sessions.pop(user_id, None)

    def cleanup_expired(self) -> int:
        """Remove all expired sessions.

        Returns:
            Number of sessions cleaned up
        """
        now = time.time()
        expired = [uid for uid, sess in self._sessions.items() if now > sess["expire"]]
        for uid in expired:
            del self._sessions[uid]
        return len(expired)

    def extend_session(self, user_id: int) -> bool:
        """Extend session expiration time.

        Args:
            user_id: QQ user ID

        Returns:
            True if session was extended, False if not found
        """
        session = self._sessions.get(user_id)
        if session:
            session["expire"] = time.time() + self._timeout
            return True
        return False
