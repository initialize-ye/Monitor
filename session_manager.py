"""User session state management for interactive menus."""

import asyncio
import time
from typing import Any


class SessionManager:
    """Manage user session states with automatic expiration.

    Thread-safe implementation using asyncio.Lock for concurrent access.
    """

    def __init__(self, timeout_seconds: int = 300, max_sessions: int = 1000):
        """Initialize session manager.

        Args:
            timeout_seconds: Session expiration time in seconds (default 5 minutes)
            max_sessions: Maximum number of concurrent sessions (default 1000)
        """
        self._sessions: dict[int, dict[str, Any]] = {}
        self._timeout = timeout_seconds
        self._max_sessions = max_sessions
        self._lock = asyncio.Lock()
        self._expired_users: set[int] = set()  # Track users whose sessions expired

    async def set_state(self, user_id: int, state: str, **kwargs) -> bool:
        """Set user session state with additional context.

        Args:
            user_id: QQ user ID
            state: State identifier (e.g., "awaiting_keyword", "menu_status")
            **kwargs: Additional context data (e.g., group_id, action)

        Returns:
            True if state was set, False if max sessions reached
        """
        async with self._lock:
            if len(self._sessions) >= self._max_sessions and user_id not in self._sessions:
                return False

            self._sessions[user_id] = {
                "state": state,
                "expire": time.time() + self._timeout,
                **kwargs
            }
            # Clear expired flag when user starts new session
            self._expired_users.discard(user_id)
            return True

    async def get_state(self, user_id: int) -> dict[str, Any] | None:
        """Get user session state if not expired.

        Args:
            user_id: QQ user ID

        Returns:
            Session dict or None if expired/not found
        """
        async with self._lock:
            session = self._sessions.get(user_id)
            if not session:
                return None

            if time.time() > session["expire"]:
                del self._sessions[user_id]
                self._expired_users.add(user_id)  # Mark as expired
                return None

            return session.copy()  # Return copy to prevent external modification

    async def clear_state(self, user_id: int) -> None:
        """Clear user session state.

        Args:
            user_id: QQ user ID
        """
        async with self._lock:
            self._sessions.pop(user_id, None)
            self._expired_users.discard(user_id)

    async def check_and_clear_expired_flag(self, user_id: int) -> bool:
        """Check if user's session expired and clear the flag.

        Args:
            user_id: QQ user ID

        Returns:
            True if session had expired, False otherwise
        """
        async with self._lock:
            if user_id in self._expired_users:
                self._expired_users.discard(user_id)
                return True
            return False

    async def cleanup_expired(self) -> int:
        """Remove all expired sessions.

        Returns:
            Number of sessions cleaned up
        """
        async with self._lock:
            now = time.time()
            expired = [uid for uid, sess in self._sessions.items() if now > sess["expire"]]
            for uid in expired:
                del self._sessions[uid]
                self._expired_users.add(uid)
            return len(expired)

    async def extend_session(self, user_id: int) -> bool:
        """Extend session expiration time.

        Args:
            user_id: QQ user ID

        Returns:
            True if session was extended, False if not found
        """
        async with self._lock:
            session = self._sessions.get(user_id)
            if session:
                session["expire"] = time.time() + self._timeout
                return True
            return False

    async def get_session_count(self) -> int:
        """Get current number of active sessions.

        Returns:
            Number of active sessions
        """
        async with self._lock:
            return len(self._sessions)
