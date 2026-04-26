from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .teams import TEAMS


SCHEMA_VERSION = 2


class NotFoundError(Exception):
    pass


class AmbiguousTeamError(Exception):
    def __init__(self, query: str, matches: list[dict[str, Any]]):
        self.query = query
        self.matches = matches
        rendered = ", ".join(f"{m['abbr']} ({m['full_name']})" for m in matches)
        super().__init__(f"Team query '{query}' is ambiguous: {rendered}")


PLAYER_FIELDS: tuple[str, ...] = (
    "player_id",
    "full_name",
    "first_name",
    "last_name",
    "team",
    "position",
    "fantasy_positions",  # JSON array stored as TEXT
    "number",
    "depth_chart_position",
    "depth_chart_order",
    "status",
    "injury_status",
    "injury_body_part",
    "injury_notes",
    "practice_participation",
    "age",
    "birth_date",
    "height",
    "weight",
    "years_exp",
    "college",
    "espn_id",
    "yahoo_id",
    "rotowire_id",
    "sportradar_id",
    "updated_at",
)


SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
  abbr TEXT PRIMARY KEY,
  full_name TEXT NOT NULL,
  conference TEXT NOT NULL,
  division TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
  player_id TEXT PRIMARY KEY,
  full_name TEXT,
  first_name TEXT,
  last_name TEXT,
  team TEXT,
  position TEXT,
  fantasy_positions TEXT,
  number INTEGER,
  depth_chart_position TEXT,
  depth_chart_order INTEGER,
  status TEXT,
  injury_status TEXT,
  injury_body_part TEXT,
  injury_notes TEXT,
  practice_participation TEXT,
  age INTEGER,
  birth_date TEXT,
  height TEXT,
  weight TEXT,
  years_exp INTEGER,
  college TEXT,
  espn_id TEXT,
  yahoo_id TEXT,
  rotowire_id TEXT,
  sportradar_id TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_players_team ON players(team);
CREATE INDEX IF NOT EXISTS idx_players_position ON players(position);
CREATE INDEX IF NOT EXISTS idx_players_name ON players(full_name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  player_id TEXT NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_player ON notes(player_id, created_at DESC);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  players_written INTEGER,
  source_url TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _team_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "abbr": row["abbr"],
        "full_name": row["full_name"],
        "conference": row["conference"],
        "division": row["division"],
    }


def _player_row(row: sqlite3.Row) -> dict[str, Any]:
    return {field: row[field] for field in PLAYER_FIELDS}


def _note_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "player_id": row["player_id"],
        "body": row["body"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _sync_run_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "players_written": row["players_written"],
        "source_url": row["source_url"],
        "status": row["status"],
        "error": row["error"],
    }


class Database:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        self._seed_teams()

    @classmethod
    def open(cls, path: str | Path | None = None) -> "Database":
        resolved = cls.resolve_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(resolved))
        return cls(conn)

    @staticmethod
    def resolve_path(path: str | Path | None = None) -> Path:
        if path is not None:
            return Path(path).expanduser()
        env = os.environ.get("FFPRESNAP_DB")
        if env:
            return Path(env).expanduser()
        return Path.home() / ".ffpresnap" / "notes.db"

    # --- migrations ---

    def _migrate(self) -> None:
        # Read current version, defaulting to 0 for fresh or pre-meta databases.
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(row["value"]) if row else 0
        except sqlite3.OperationalError:
            current = 0

        if current >= SCHEMA_VERSION:
            # Ensure new tables exist on a partially-built DB but never touch existing rows.
            self.conn.executescript(SCHEMA_V2)
            self.conn.commit()
            return

        # Upgrading from anything older than v2: drop legacy tables and rebuild.
        self.conn.executescript(
            """
            DROP TABLE IF EXISTS notes;
            DROP TABLE IF EXISTS players;
            """
        )
        self.conn.executescript(SCHEMA_V2)
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def _seed_teams(self) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO teams (abbr, full_name, conference, division) "
            "VALUES (?, ?, ?, ?)",
            TEAMS,
        )
        self.conn.commit()

    # --- teams ---

    def list_teams(self, query: str | None = None) -> list[dict[str, Any]]:
        if query:
            like = f"%{query}%"
            rows = self.conn.execute(
                "SELECT * FROM teams "
                "WHERE abbr LIKE ? COLLATE NOCASE "
                "   OR full_name LIKE ? COLLATE NOCASE "
                "   OR conference LIKE ? COLLATE NOCASE "
                "   OR division LIKE ? COLLATE NOCASE "
                "ORDER BY conference, division, full_name",
                (like, like, like, like),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM teams ORDER BY conference, division, full_name"
            ).fetchall()
        return [_team_row(r) for r in rows]

    def get_team(self, identifier: str) -> dict[str, Any]:
        ident = identifier.strip()
        if not ident:
            raise NotFoundError("Empty team identifier")

        # 1. Exact abbreviation match.
        row = self.conn.execute(
            "SELECT * FROM teams WHERE abbr = ? COLLATE NOCASE", (ident,)
        ).fetchone()
        if row is not None:
            return _team_row(row)

        # 2. Exact full-name match.
        row = self.conn.execute(
            "SELECT * FROM teams WHERE full_name = ? COLLATE NOCASE", (ident,)
        ).fetchone()
        if row is not None:
            return _team_row(row)

        # 3. Substring/suffix match on full_name.
        rows = self.conn.execute(
            "SELECT * FROM teams WHERE full_name LIKE ? COLLATE NOCASE",
            (f"%{ident}%",),
        ).fetchall()
        if not rows:
            raise NotFoundError(f"No team matches '{identifier}'")
        if len(rows) > 1:
            raise AmbiguousTeamError(identifier, [_team_row(r) for r in rows])
        return _team_row(rows[0])

    # --- players ---

    def replace_players(self, rows: list[dict[str, Any]]) -> int:
        """Atomically replace the players set. Notes for missing players cascade-delete.

        Returns the number of rows written.
        """
        now = _now()
        prepared: list[tuple[Any, ...]] = []
        seen_ids: set[str] = set()
        for row in rows:
            pid = row.get("player_id")
            if not pid:
                raise ValueError("player_id is required on every row")
            pid = str(pid)
            if pid in seen_ids:
                raise ValueError(f"Duplicate player_id in input: {pid}")
            seen_ids.add(pid)
            values = []
            for field in PLAYER_FIELDS:
                if field == "player_id":
                    values.append(pid)
                elif field == "updated_at":
                    values.append(now)
                else:
                    values.append(row.get(field))
            prepared.append(tuple(values))

        placeholders = ", ".join("?" for _ in PLAYER_FIELDS)
        columns = ", ".join(PLAYER_FIELDS)
        update_cols = [f for f in PLAYER_FIELDS if f != "player_id"]
        update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
        upsert_sql = (
            f"INSERT INTO players ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(player_id) DO UPDATE SET {update_clause}"
        )
        try:
            self.conn.execute("BEGIN")
            # Drop only players absent from the new set so notes for surviving
            # players are preserved. Use real UPSERT (not INSERT OR REPLACE) so
            # updating an existing row does not cascade-delete its notes.
            if seen_ids:
                marks = ",".join("?" for _ in seen_ids)
                self.conn.execute(
                    f"DELETE FROM players WHERE player_id NOT IN ({marks})",
                    tuple(seen_ids),
                )
            else:
                self.conn.execute("DELETE FROM players")
            if prepared:
                self.conn.executemany(upsert_sql, prepared)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(prepared)

    def get_player(self, player_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM players WHERE player_id = ?", (str(player_id),)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Player {player_id!r} not found")
        return _player_row(row)

    def list_players(
        self, team: str | None = None, position: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if team:
            clauses.append("team = ? COLLATE NOCASE")
            params.append(team)
        if position:
            clauses.append("position = ? COLLATE NOCASE")
            params.append(position)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM players {where} ORDER BY full_name COLLATE NOCASE",
            tuple(params),
        ).fetchall()
        return [_player_row(r) for r in rows]

    def find_players(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM players WHERE full_name LIKE ? "
            "ORDER BY full_name COLLATE NOCASE LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [_player_row(r) for r in rows]

    def depth_chart(self, team_abbr: str) -> list[dict[str, Any]]:
        """Players for a team, ordered by depth_chart_position then depth_chart_order.

        Players with NULL depth_chart_position are returned at the end, after ranked rows.
        """
        rows = self.conn.execute(
            "SELECT * FROM players WHERE team = ? COLLATE NOCASE "
            "ORDER BY "
            "  CASE WHEN depth_chart_position IS NULL THEN 1 ELSE 0 END, "
            "  depth_chart_position COLLATE NOCASE, "
            "  CASE WHEN depth_chart_order IS NULL THEN 1 ELSE 0 END, "
            "  depth_chart_order, "
            "  full_name COLLATE NOCASE",
            (team_abbr,),
        ).fetchall()
        return [_player_row(r) for r in rows]

    # --- notes ---

    def add_note(self, player_id: str, body: str) -> dict[str, Any]:
        self.get_player(player_id)
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO notes (player_id, body, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (str(player_id), body, now, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM notes WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _note_row(row)

    def list_notes(self, player_id: str) -> list[dict[str, Any]]:
        self.get_player(player_id)
        rows = self.conn.execute(
            "SELECT * FROM notes WHERE player_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (str(player_id),),
        ).fetchall()
        return [_note_row(r) for r in rows]

    def update_note(self, note_id: int, body: str) -> dict[str, Any]:
        cur = self.conn.execute(
            "UPDATE notes SET body = ?, updated_at = ? WHERE id = ?",
            (body, _now(), note_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            raise NotFoundError(f"Note {note_id} not found")
        row = self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return _note_row(row)

    def delete_note(self, note_id: int) -> None:
        cur = self.conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self.conn.commit()
        if cur.rowcount == 0:
            raise NotFoundError(f"Note {note_id} not found")

    # --- sync runs ---

    def record_sync_start(self, source_url: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO sync_runs (started_at, source_url, status) "
            "VALUES (?, ?, 'running')",
            (_now(), source_url),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_sync_finish(
        self,
        run_id: int,
        players_written: int,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        self.conn.execute(
            "UPDATE sync_runs SET finished_at = ?, players_written = ?, "
            "status = ?, error = ? WHERE id = ?",
            (_now(), players_written, status, error, run_id),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM sync_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"sync_run {run_id} not found")
        return _sync_run_row(row)

    def last_sync(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _sync_run_row(row) if row else None

    def close(self) -> None:
        self.conn.close()
