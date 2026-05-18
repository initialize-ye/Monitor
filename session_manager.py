"""用户会话状态管理，用于交互菜单。"""

import asyncio
import time
from typing import Any


class SessionManager:
    """用户会话状态管理，支持自动过期。使用 asyncio.Lock 保证线程安全。"""

    def __init__(self, timeout_seconds: int = 300, max_sessions: int = 1000):
        """初始化会话管理器。

        Args:
            timeout_seconds: 会话过期时间（秒），默认 5 分钟
            max_sessions: 最大并发会话数，默认 1000
        """
        self._sessions: dict[int, dict[str, Any]] = {}
        self._timeout = timeout_seconds
        self._max_sessions = max_sessions
        self._lock = asyncio.Lock()
        self._expired_users: set[int] = set()  # Track users whose sessions expired

    async def set_state(self, user_id: int, state: str, **kwargs) -> bool:
        """设置用户会话状态。

        Args:
            user_id: QQ 用户 ID
            state: 状态标识（如 "awaiting_keyword"、"menu_status"）
            **kwargs: 附加上下文数据（如 group_id、action）

        Returns:
            设置成功返回 True，超过最大会话数返回 False
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
        """获取用户会话状态（未过期时）。

        Args:
            user_id: QQ 用户 ID

        Returns:
            会话字典，过期或不存在返回 None
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
        """清除用户会话状态。

        Args:
            user_id: QQ 用户 ID
        """
        async with self._lock:
            self._sessions.pop(user_id, None)
            self._expired_users.discard(user_id)

    async def check_and_clear_expired_flag(self, user_id: int) -> bool:
        """检查用户会话是否已过期并清除标记。

        Args:
            user_id: QQ 用户 ID

        Returns:
            会话已过期返回 True，否则返回 False
        """
        async with self._lock:
            if user_id in self._expired_users:
                self._expired_users.discard(user_id)
                return True
            return False

    async def get_state_or_check_expired(self, user_id: int) -> tuple[dict[str, Any] | None, bool]:
        """原子操作：获取会话状态或检查是否已过期。

        合并 get_state 和 check_and_clear_expired_flag 为单次原子操作，
        避免 TOCTOU 竞态条件。

        Args:
            user_id: QQ 用户 ID

        Returns:
            (会话或None, 是否已过期)。
            会话有效: (session, False)
            会话已过期: (None, True)
            无会话: (None, False)
        """
        async with self._lock:
            session = self._sessions.get(user_id)
            if session:
                if time.time() > session["expire"]:
                    del self._sessions[user_id]
                    self._expired_users.discard(user_id)
                    return None, True
                return session.copy(), False
            if user_id in self._expired_users:
                self._expired_users.discard(user_id)
                return None, True
            return None, False

    async def cleanup_expired(self) -> int:
        """清理所有过期会话。

        Returns:
            清理的会话数量
        """
        async with self._lock:
            now = time.time()
            expired = [uid for uid, sess in self._sessions.items() if now > sess["expire"]]
            for uid in expired:
                del self._sessions[uid]
                self._expired_users.add(uid)
            return len(expired)

    async def extend_session(self, user_id: int) -> bool:
        """延长会话过期时间。

        Args:
            user_id: QQ 用户 ID

        Returns:
            延长成功返回 True，会话不存在返回 False
        """
        async with self._lock:
            session = self._sessions.get(user_id)
            if session:
                session["expire"] = time.time() + self._timeout
                return True
            return False

    async def get_session_count(self) -> int:
        """获取当前活跃会话数。

        Returns:
            活跃会话数量
        """
        async with self._lock:
            return len(self._sessions)
