from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audit_context import audit_actor


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path, bot_id: str = "default") -> None:
        self.bot_id = bot_id
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self, allow_privileged_roles: bool = False) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS managed_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS uploads (
            id TEXT PRIMARY KEY,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            content_type TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reaction_roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            emoji_key TEXT NOT NULL,
            emoji_display TEXT NOT NULL,
            role_id TEXT NOT NULL,
            remove_on_unreact INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(message_id, emoji_key)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
        with self._lock, self._connect() as connection:
            connection.executescript(schema)
            # Rebuild legacy tables transactionally to replace global uniqueness.
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("""CREATE TABLE IF NOT EXISTS bot_profiles (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, application_id TEXT,
                    token_env TEXT NOT NULL, enabled INTEGER NOT NULL,
                    allow_privileged_roles INTEGER NOT NULL DEFAULT 0)""")
                connection.execute("""INSERT OR IGNORE INTO bot_profiles
                    VALUES ('default', 'Основной бот', NULL, 'DISCORD_BOT_TOKEN', 1, ?)""",
                    (int(allow_privileged_roles),))
                for table in ("managed_messages", "uploads", "reaction_roles", "audit_log"):
                    columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
                    if any(column["name"] == "bot_id" for column in columns):
                        continue
                    definition = connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()[0]
                    definition = definition.replace(f"CREATE TABLE {table}", f"CREATE TABLE {table}_new", 1)
                    definition = definition.replace("message_id TEXT NOT NULL UNIQUE", "message_id TEXT NOT NULL")
                    definition = definition.replace("UNIQUE(message_id, emoji_key)", "UNIQUE(bot_id, message_id, emoji_key)")
                    sequence = connection.execute(
                        "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
                    ).fetchone()
                    opening = definition.index("(")
                    definition = definition[:opening + 1] + "bot_id TEXT NOT NULL REFERENCES bot_profiles(id), " + definition[opening + 1:]
                    end = definition.rfind(")")
                    extra = ""
                    if table == "managed_messages":
                        extra += ", UNIQUE(bot_id, message_id)"
                    definition = definition[:end] + extra + definition[end:]
                    connection.execute(definition)
                    names = ", ".join(column["name"] for column in columns)
                    connection.execute(f"INSERT INTO {table}_new ({names}, bot_id) SELECT {names}, 'default' FROM {table}")
                    connection.execute(f"DROP TABLE {table}")
                    connection.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
                    if sequence:
                        connection.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
                        connection.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, sequence[0]))
                for table in ("managed_messages", "uploads", "reaction_roles", "audit_log"):
                    connection.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_bot ON {table}(bot_id)")
                # v2: panel identities, access grants and opaque session hashes.
                connection.execute("""CREATE TABLE IF NOT EXISTS panel_users (
                    id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    blocked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    last_login_at TEXT)""")
                connection.execute("""CREATE TABLE IF NOT EXISTS bot_access (
                    user_id TEXT NOT NULL REFERENCES panel_users(id),
                    bot_id TEXT NOT NULL REFERENCES bot_profiles(id),
                    role TEXT NOT NULL CHECK(role IN ('viewer', 'operator')),
                    PRIMARY KEY(user_id, bot_id))""")
                connection.execute("""CREATE TABLE IF NOT EXISTS panel_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES panel_users(id),
                    expires_at INTEGER NOT NULL)""")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_panel_sessions_user ON panel_sessions(user_id)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_panel_sessions_expiry ON panel_sessions(expires_at)")
                if not any(column["name"] == "actor_id" for column in connection.execute("PRAGMA table_info(audit_log)")):
                    connection.execute("ALTER TABLE audit_log ADD COLUMN actor_id TEXT")
                connection.execute("PRAGMA user_version = 2")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def bootstrap_profiles(
        self, configured: list[tuple[str, str, str]], legacy_token_configured: bool
    ) -> None:
        """Discover built-in env references without replacing user profiles or data."""
        if not configured:
            return
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for bot_id, name, token_env in configured:
                connection.execute(
                    """INSERT OR IGNORE INTO bot_profiles
                       (id, name, application_id, token_env, enabled, allow_privileged_roles)
                       SELECT ?, ?, NULL, ?, 1, 0
                       WHERE NOT EXISTS (SELECT 1 FROM bot_profiles WHERE token_env = ?)""",
                    (bot_id, name, token_env, token_env),
                )
            # Remove only the unused automatic placeholder. Legacy data and
            # explicitly edited profiles keep their owner and settings.
            placeholder = connection.execute(
                """SELECT 1 FROM bot_profiles WHERE id = 'default'
                   AND name = 'Основной бот' AND token_env = 'DISCORD_BOT_TOKEN'
                   AND application_id IS NULL AND enabled = 1"""
            ).fetchone()
            has_data = any(
                connection.execute(f"SELECT 1 FROM {table} WHERE bot_id = 'default' LIMIT 1").fetchone()
                for table in ("managed_messages", "uploads", "reaction_roles", "audit_log")
            )
            has_other_profile = connection.execute(
                "SELECT 1 FROM bot_profiles WHERE id != 'default' LIMIT 1"
            ).fetchone()
            has_access = connection.execute("SELECT 1 FROM bot_access WHERE bot_id = 'default' LIMIT 1").fetchone()
            if placeholder and has_other_profile and not legacy_token_configured and not has_data and not has_access:
                connection.execute("DELETE FROM bot_profiles WHERE id = 'default'")
            connection.commit()

    def scoped(self, bot_id: str) -> "Database":
        return Database(self.path, bot_id)

    def profiles(self) -> list[dict[str, Any]]:
        return self.fetch_all("SELECT * FROM bot_profiles ORDER BY id")

    def save_profile(self, profile: dict[str, Any]) -> None:
        self.execute("""INSERT INTO bot_profiles
            (id, name, application_id, token_env, enabled, allow_privileged_roles)
            VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, application_id=excluded.application_id,
            token_env=excluded.token_env, enabled=excluded.enabled,
            allow_privileged_roles=excluded.allow_privileged_roles""",
            tuple(profile[key] for key in ("id", "name", "application_id", "token_env", "enabled", "allow_privileged_roles")))

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(sql, params)
            connection.commit()
            return int(cursor.lastrowid)

    def fetch_one(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(sql, params).fetchone()
            return dict(row) if row else None

    def fetch_all(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def add_message(
        self, guild_id: int, channel_id: int, message_id: int, payload: dict[str, Any]
    ) -> int:
        now = utc_now()
        return self.execute(
            """
            INSERT INTO managed_messages
                (bot_id, guild_id, channel_id, message_id, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.bot_id,
                str(guild_id),
                str(channel_id),
                str(message_id),
                json.dumps(payload, ensure_ascii=False),
                now,
                now,
            ),
        )

    def update_message(self, record_id: int, payload: dict[str, Any]) -> None:
        self.execute(
            "UPDATE managed_messages SET payload_json = ?, updated_at = ? WHERE id = ? AND bot_id = ?",
            (json.dumps(payload, ensure_ascii=False), utc_now(), record_id, self.bot_id),
        )

    def get_message(self, record_id: int) -> dict[str, Any] | None:
        row = self.fetch_one("SELECT * FROM managed_messages WHERE id = ? AND bot_id = ?", (record_id, self.bot_id))
        return self._decode_message(row) if row else None

    def get_message_by_discord_id(self, message_id: int | str) -> dict[str, Any] | None:
        row = self.fetch_one(
            "SELECT * FROM managed_messages WHERE message_id = ? AND bot_id = ?", (str(message_id), self.bot_id)
        )
        return self._decode_message(row) if row else None

    def upsert_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        payload: dict[str, Any],
    ) -> tuple[int, bool]:
        existing = self.get_message_by_discord_id(message_id)
        if existing:
            self.update_message(int(existing["id"]), payload)
            return int(existing["id"]), False
        return self.add_message(guild_id, channel_id, message_id, payload), True

    def list_messages(self) -> list[dict[str, Any]]:
        rows = self.fetch_all("SELECT * FROM managed_messages WHERE bot_id = ? ORDER BY id DESC", (self.bot_id,))
        return [self._decode_message(row) for row in rows]

    @staticmethod
    def _decode_message(row: dict[str, Any]) -> dict[str, Any]:
        row["payload"] = json.loads(row.pop("payload_json"))
        return row

    def delete_message(self, record_id: int) -> None:
        self.execute("DELETE FROM managed_messages WHERE id = ? AND bot_id = ?", (record_id, self.bot_id))

    def add_upload(
        self,
        upload_id: str,
        original_name: str,
        stored_name: str,
        content_type: str | None,
        size_bytes: int,
    ) -> None:
        self.execute(
            """
            INSERT INTO uploads
                (bot_id, id, original_name, stored_name, content_type, size_bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.bot_id,
                upload_id,
                original_name,
                stored_name,
                content_type,
                size_bytes,
                utc_now(),
            ),
        )

    def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM uploads WHERE id = ? AND bot_id = ?", (upload_id, self.bot_id))

    def add_reaction_role(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        emoji_key: str,
        emoji_display: str,
        role_id: int,
        remove_on_unreact: bool,
    ) -> int:
        return self.execute(
            """
            INSERT INTO reaction_roles
                (bot_id, guild_id, channel_id, message_id, emoji_key, emoji_display,
                 role_id, remove_on_unreact, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.bot_id,
                str(guild_id),
                str(channel_id),
                str(message_id),
                emoji_key,
                emoji_display,
                str(role_id),
                int(remove_on_unreact),
                utc_now(),
            ),
        )

    def list_reaction_roles(self) -> list[dict[str, Any]]:
        return self.fetch_all("SELECT * FROM reaction_roles WHERE bot_id = ? ORDER BY id DESC", (self.bot_id,))

    def get_reaction_role(self, record_id: int) -> dict[str, Any] | None:
        return self.fetch_one("SELECT * FROM reaction_roles WHERE id = ? AND bot_id = ?", (record_id, self.bot_id))

    def find_reaction_role(
        self, message_id: int, emoji_key: str
    ) -> dict[str, Any] | None:
        return self.fetch_one(
            "SELECT * FROM reaction_roles WHERE message_id = ? AND emoji_key = ? AND bot_id = ?",
            (str(message_id), emoji_key, self.bot_id),
        )

    def delete_reaction_role(self, record_id: int) -> None:
        self.execute("DELETE FROM reaction_roles WHERE id = ? AND bot_id = ?", (record_id, self.bot_id))

    def delete_reaction_roles_for_message(self, message_id: str | int) -> None:
        self.execute(
            "DELETE FROM reaction_roles WHERE message_id = ? AND bot_id = ?", (str(message_id), self.bot_id)
        )

    def audit(self, action: str, details: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO audit_log (bot_id, action, details_json, created_at, actor_id) VALUES (?, ?, ?, ?, ?)",
            (self.bot_id, action, json.dumps(details, ensure_ascii=False), utc_now(), audit_actor.get()),
        )

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.fetch_all(
            "SELECT * FROM audit_log WHERE bot_id = ? ORDER BY id DESC LIMIT ?", (self.bot_id, max(1, min(limit, 500)))
        )
        for row in rows:
            row["details"] = json.loads(row.pop("details_json"))
        return rows
