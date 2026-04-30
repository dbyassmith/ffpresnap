from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import prompt_loader
from ._naming import normalize_full_name, synthesize_ourlads_id
from .teams import TEAMS


SCHEMA_VERSION = 8


class NotFoundError(Exception):
    pass


class ConcurrentSyncError(Exception):
    """Raised when another sync run is already in flight (advisory lock)."""

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
  updated_at TEXT NOT NULL,
  watchlist INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'sleeper',
  ourlads_id TEXT,
  depth_chart_last_observed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_players_team ON players(team);
CREATE INDEX IF NOT EXISTS idx_players_position ON players(position);
CREATE INDEX IF NOT EXISTS idx_players_name ON players(full_name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_type TEXT NOT NULL CHECK (subject_type IN ('player', 'team', 'study')),
  subject_id TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_subject ON notes(subject_type, subject_id, created_at DESC);

CREATE TABLE IF NOT EXISTS studies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL CHECK (status IN ('open', 'archived')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_studies_status ON studies(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS note_player_mentions (
  note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  player_id TEXT NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
  PRIMARY KEY (note_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_npm_player ON note_player_mentions(player_id);

CREATE TABLE IF NOT EXISTS note_team_mentions (
  note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  team_abbr TEXT NOT NULL REFERENCES teams(abbr),
  PRIMARY KEY (note_id, team_abbr)
);

CREATE INDEX IF NOT EXISTS idx_ntm_team ON note_team_mentions(team_abbr);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  players_written INTEGER,
  source_url TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  source TEXT NOT NULL DEFAULT 'sleeper',
  items_fetched INTEGER,
  items_new INTEGER,
  items_matched INTEGER,
  items_unmatched INTEGER
);

CREATE TABLE IF NOT EXISTS feed_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  source_url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feed_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES feed_sources(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  external_player_id TEXT,
  external_player_name TEXT NOT NULL,
  external_team TEXT,
  external_position TEXT,
  team_abbr TEXT,
  source_url TEXT,
  source_author TEXT,
  raw_html TEXT,
  cleaned_text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  player_id TEXT REFERENCES players(player_id) ON DELETE SET NULL,
  note_id INTEGER REFERENCES notes(id) ON DELETE SET NULL,
  note_run_id INTEGER,
  UNIQUE(source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_feed_items_player ON feed_items(player_id);
CREATE INDEX IF NOT EXISTS idx_feed_items_source_created
  ON feed_items(source_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feed_items_unmatched
  ON feed_items(ingested_at)
  WHERE player_id IS NULL;

CREATE TABLE IF NOT EXISTS prompts (
  slug TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  body TEXT NOT NULL,
  updated_at TEXT NOT NULL
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
    out = {field: row[field] for field in PLAYER_FIELDS}
    out["watchlist"] = bool(row["watchlist"])
    out["source"] = row["source"]
    out["ourlads_id"] = row["ourlads_id"]
    out["depth_chart_last_observed_at"] = row["depth_chart_last_observed_at"]
    return out


def _note_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "body": row["body"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _study_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }




def _sync_run_row(row: sqlite3.Row) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "players_written": row["players_written"],
        "source_url": row["source_url"],
        "status": row["status"],
        "error": row["error"],
        "source": row["source"],
    }
    # Feed-sync counter columns (NULL on player-data syncs).
    for col in ("items_fetched", "items_new", "items_matched", "items_unmatched"):
        try:
            out[col] = row[col]
        except (IndexError, KeyError):
            out[col] = None
    return out


def _feed_item_row(row: sqlite3.Row, *, source_name: str | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "source_name": source_name,
        "external_id": row["external_id"],
        "external_player_id": row["external_player_id"],
        "external_player_name": row["external_player_name"],
        "external_team": row["external_team"],
        "external_position": row["external_position"],
        "team_abbr": row["team_abbr"],
        "source_url": row["source_url"],
        "source_author": row["source_author"],
        "cleaned_text": row["cleaned_text"],
        "created_at": row["created_at"],
        "ingested_at": row["ingested_at"],
        "player_id": row["player_id"],
        "note_id": row["note_id"],
        "note_run_id": row["note_run_id"],
    }


class Database:
    def __init__(
        self, conn: sqlite3.Connection, *, path: str | Path | None = None
    ):
        self.conn = conn
        self.path = Path(path) if path is not None else None
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        self._seed_teams()
        self._seed_prompts()
        self._seed_feed_sources()

    @classmethod
    def open(cls, path: str | Path | None = None) -> "Database":
        resolved = cls.resolve_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(resolved))
        return cls(conn, path=resolved)

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
            self.conn.executescript(SCHEMA_V2)
            self.conn.commit()
            return

        if current < 2:
            # Pre-v2 (or fresh): drop legacy tables and rebuild from scratch.
            self.conn.executescript(
                """
                DROP TABLE IF EXISTS notes;
                DROP TABLE IF EXISTS players;
                """
            )
            self.conn.executescript(SCHEMA_V2)
            self._set_schema_version(SCHEMA_VERSION)
            self.conn.commit()
            return

        # v2 -> v3: migrate notes to the polymorphic (subject_type, subject_id) shape.
        if current < 3:
            self.conn.executescript(
                """
                ALTER TABLE notes RENAME TO notes_v2;

                CREATE TABLE notes (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  subject_type TEXT NOT NULL CHECK (subject_type IN ('player', 'team')),
                  subject_id TEXT NOT NULL,
                  body TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                INSERT INTO notes (id, subject_type, subject_id, body, created_at, updated_at)
                  SELECT id, 'player', player_id, body, created_at, updated_at FROM notes_v2;

                DROP TABLE notes_v2;

                CREATE INDEX IF NOT EXISTS idx_notes_subject
                  ON notes(subject_type, subject_id, created_at DESC);
                """
            )

        # v5 -> v6: add `watchlist INTEGER NOT NULL DEFAULT 0` to players. SQLite
        # ALTER TABLE ADD COLUMN supports a DEFAULT, so existing rows get 0
        # without a table rebuild. Sync's UPSERT excludes watchlist from its
        # update set, preserving user toggles across re-syncs.
        if current >= 4 and current < 6:
            cols = self.conn.execute("PRAGMA table_info(players)").fetchall()
            # Only ALTER if the players table exists already; if it doesn't,
            # SCHEMA_V2 below creates it fresh with the watchlist column.
            if cols and not any(c["name"] == "watchlist" for c in cols):
                self.conn.execute(
                    "ALTER TABLE players ADD COLUMN watchlist INTEGER NOT NULL DEFAULT 0"
                )

        # v6 -> v7: add multi-source tracking columns. Three new columns on
        # players (source, ourlads_id, depth_chart_last_observed_at) and one on
        # sync_runs (source). All ALTER TABLE ADD COLUMN with safe defaults.
        # Each ALTER is PRAGMA-guarded so partial-prior-application is idempotent.
        # After ALTERs, run an UPDATE ... WHERE source IS NULL to defend against
        # any prior hand-modification that left NULL `source` values.
        if current >= 6 and current < 7:
            player_cols = self.conn.execute("PRAGMA table_info(players)").fetchall()
            player_names = {c["name"] for c in player_cols}
            if player_cols and "source" not in player_names:
                self.conn.execute(
                    "ALTER TABLE players ADD COLUMN source TEXT NOT NULL DEFAULT 'sleeper'"
                )
            if player_cols and "ourlads_id" not in player_names:
                self.conn.execute(
                    "ALTER TABLE players ADD COLUMN ourlads_id TEXT"
                )
            if player_cols and "depth_chart_last_observed_at" not in player_names:
                self.conn.execute(
                    "ALTER TABLE players ADD COLUMN depth_chart_last_observed_at TEXT"
                )
            sync_cols = self.conn.execute(
                "PRAGMA table_info(sync_runs)"
            ).fetchall()
            sync_names = {c["name"] for c in sync_cols}
            if sync_cols and "source" not in sync_names:
                self.conn.execute(
                    "ALTER TABLE sync_runs ADD COLUMN source TEXT NOT NULL DEFAULT 'sleeper'"
                )
            # NULL-source backfill defends against partial-prior-state.
            self.conn.execute(
                "UPDATE players SET source = 'sleeper' WHERE source IS NULL"
            )
            self.conn.execute(
                "UPDATE sync_runs SET source = 'sleeper' WHERE source IS NULL"
            )

        # v7 -> v8: add feed_sources + feed_items tables (created by the
        # SCHEMA_V2 executescript at the bottom of _migrate) plus four
        # nullable feed counter columns on sync_runs. Each column ALTER is
        # PRAGMA-guarded so partial-prior-application is idempotent.
        if current >= 7 and current < 8:
            sync_cols = self.conn.execute(
                "PRAGMA table_info(sync_runs)"
            ).fetchall()
            sync_names = {c["name"] for c in sync_cols}
            for col in (
                "items_fetched",
                "items_new",
                "items_matched",
                "items_unmatched",
            ):
                if sync_cols and col not in sync_names:
                    self.conn.execute(
                        f"ALTER TABLE sync_runs ADD COLUMN {col} INTEGER"
                    )

        # v4 -> v5 is purely additive (the `prompts` table). The
        # executescript(SCHEMA_V2) call below creates it; this arm exists for
        # symmetry and to make schema-version progression observable in tests.
        # (Empty body intentional.)

        # v3 -> v4: rebuild notes to extend subject_type CHECK to include 'study',
        # and create the studies + mentions tables.
        if current < 4:
            self.conn.executescript(
                """
                ALTER TABLE notes RENAME TO notes_v3;

                CREATE TABLE notes (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  subject_type TEXT NOT NULL CHECK (subject_type IN ('player', 'team', 'study')),
                  subject_id TEXT NOT NULL,
                  body TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                INSERT INTO notes (id, subject_type, subject_id, body, created_at, updated_at)
                  SELECT id, subject_type, subject_id, body, created_at, updated_at FROM notes_v3;

                DROP TABLE notes_v3;
                """
            )

        # Ensure all v4 peer tables and indexes exist.
        self.conn.executescript(SCHEMA_V2)
        self._set_schema_version(SCHEMA_VERSION)
        self.conn.commit()

    def _set_schema_version(self, version: int) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(version),),
        )

    def _seed_feed_sources(self) -> None:
        """Seed the feed_sources catalog from the live adapter registry.

        Importing :mod:`ffpresnap.feeds` triggers adapter registration, so by
        the time this runs every concrete adapter has a (name, source_url)
        pair available. Tests that register fake adapters before opening a
        DB get them seeded automatically.
        """
        # Imported here to avoid a top-of-module circular import (feeds
        # imports nothing from db, but db is loaded earlier in __init__).
        from .feeds._registry import _REGISTRY

        sources = tuple((a.name, a.source_url) for a in _REGISTRY.values())
        if not sources:
            return
        self.conn.executemany(
            "INSERT INTO feed_sources (name, source_url) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET source_url = excluded.source_url",
            sources,
        )
        self.conn.commit()

    def _seed_teams(self) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO teams (abbr, full_name, conference, division) "
            "VALUES (?, ?, ?, ?)",
            TEAMS,
        )
        self.conn.commit()

    def _seed_prompts(self, loader=None) -> None:
        """Reconcile prompts from the repo into the DB.

        Repo is source of truth: upserts each prompt by slug, deletes rows whose
        slug is no longer present in the repo. Atomic.
        """
        load = loader if loader is not None else prompt_loader.load_prompts
        prompts = load()
        now = _now()
        try:
            self.conn.execute("BEGIN")
            for p in prompts:
                self.conn.execute(
                    "INSERT INTO prompts (slug, title, description, body, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(slug) DO UPDATE SET "
                    "  title = excluded.title, "
                    "  description = excluded.description, "
                    "  body = excluded.body, "
                    "  updated_at = excluded.updated_at",
                    (p["slug"], p["title"], p["description"], p["body"], now),
                )
            slugs = [p["slug"] for p in prompts]
            if slugs:
                marks = ",".join("?" for _ in slugs)
                self.conn.execute(
                    f"DELETE FROM prompts WHERE slug NOT IN ({marks})",
                    tuple(slugs),
                )
            else:
                self.conn.execute("DELETE FROM prompts")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

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
        """Backward-compatible wrapper around upsert_players_for_source('sleeper').
        Existing callers (tests, pre-Ourlads code paths) keep working unchanged.
        New code should call upsert_players_for_source directly.
        """
        return self.upsert_players_for_source("sleeper", rows)

    def find_player_for_match(
        self, normalized_name: str, team: str, position: str
    ) -> list[dict[str, Any]]:
        """Return all players whose normalized name + team + position match.
        Used by the Ourlads identity-merge path. Normalization happens in
        Python (NFKD + diacritic strip) so the comparison is symmetric with
        the incoming row.
        """
        rows = self.conn.execute(
            "SELECT * FROM players WHERE team = ? AND position = ?",
            (team, position),
        ).fetchall()
        return [
            _player_row(r)
            for r in rows
            if normalize_full_name(r["full_name"] or "") == normalized_name
        ]

    def upsert_players_for_source(
        self,
        source: str,
        rows: list[dict[str, Any]],
        *,
        completeness: dict[str, bool] | None = None,
        run_start_at: str | None = None,
    ) -> int:
        """Upsert player rows scoped by source. Returns rows successfully
        written or merged.

        For ``source='sleeper'`` the rows are expected to carry ``player_id``
        (Sleeper provides stable ids). For each row:
          - If no existing row with that player_id, INSERT with source='sleeper'.
          - If existing row has source='sleeper', full UPSERT (overwrites all
            sync-managed fields).
          - If existing row has source IN ('ourlads', 'merged'), UPSERT but
            **skip** depth_chart_position / depth_chart_order writes (per-field
            ownership: Ourlads owns depth chart on rows it has touched).
        After upserts, source-scoped DELETE removes any source='sleeper' rows
        no longer in the input set, plus orphan-note cleanup.

        For ``source='ourlads'`` each row carries ``team``, ``full_name``,
        ``position``, optional ``ourlads_id``, optional ``number``, optional
        ``depth_chart_position``/``depth_chart_order``. Identity matching:
          - If ``ourlads_id`` provided and matches an existing row, update in
            place (bump source 'sleeper' -> 'merged' if needed).
          - Else look up by find_player_for_match. Exactly one match: update
            in place; bind ourlads_id; bump source 'sleeper' -> 'merged'.
            Zero or >1: insert new row with synthesized player_id and
            source='ourlads'. Multi-match logs an ambiguous-match line to
            stderr.
        Ourlads sync does not delete rows en masse; it only upserts. After
        upserts, when ``completeness[team]`` is True for a given team, R13
        runs: for any source IN ('ourlads','merged') row on that team whose
        ``depth_chart_last_observed_at`` is older than ``run_start_at``,
        clear depth fields. For source='merged' rows specifically, also
        demote source -> 'sleeper' and clear ourlads_id (Sleeper resumes
        ownership).
        """
        if source not in ("sleeper", "ourlads"):
            raise ValueError(f"unknown source: {source!r}")

        run_at = run_start_at or _now()
        written = 0

        try:
            self.conn.execute("BEGIN")
            if source == "sleeper":
                written = self._upsert_sleeper_rows(rows, now=run_at)
            else:
                written = self._upsert_ourlads_rows(
                    rows, completeness=completeness, run_start_at=run_at
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return written

    def _upsert_sleeper_rows(self, rows: list[dict[str, Any]], *, now: str) -> int:
        # Validate input + collect ids.
        seen_ids: set[str] = set()
        validated: list[dict[str, Any]] = []
        for row in rows:
            pid = row.get("player_id")
            if not pid:
                raise ValueError("player_id is required on every Sleeper row")
            pid = str(pid)
            if pid in seen_ids:
                raise ValueError(f"Duplicate player_id in input: {pid}")
            seen_ids.add(pid)
            validated.append({**row, "player_id": pid})

        # Pull existing source for each input id so we know which rows need
        # the Ourlads-owned-fields opt-out.
        existing_sources: dict[str, str] = {}
        if seen_ids:
            marks = ",".join("?" for _ in seen_ids)
            for r in self.conn.execute(
                f"SELECT player_id, source FROM players WHERE player_id IN ({marks})",
                tuple(seen_ids),
            ).fetchall():
                existing_sources[r["player_id"]] = r["source"]

        # Sleeper-side identity merge: for each input row whose player_id is
        # NOT already in the table but whose normalized name+team+position
        # matches an existing Ourlads-only row, transfer notes/mentions to the
        # incoming Sleeper player_id and delete the Ourlads-only row. After
        # this loop, the Sleeper UPSERT below inserts the new row in place
        # with source='merged' and the captured ourlads metadata.
        merged_metadata: dict[str, dict[str, Any]] = {}
        for row in validated:
            pid = row["player_id"]
            if pid in existing_sources:
                continue  # already in table, normal UPSERT path handles it.
            team = row.get("team")
            full_name = row.get("full_name")
            position = row.get("position")
            if not (team and full_name and position):
                continue
            normalized = normalize_full_name(full_name)
            candidates = [
                r
                for r in self.conn.execute(
                    "SELECT * FROM players WHERE team = ? AND position = ? "
                    "AND source = 'ourlads'",
                    (team, position),
                ).fetchall()
                if normalize_full_name(r["full_name"] or "") == normalized
            ]
            if len(candidates) != 1:
                if len(candidates) > 1:
                    sys.stderr.write(
                        "sleeper:identity:ambiguous: "
                        f"name={normalized} team={team} position={position} "
                        f"candidates={[c['player_id'] for c in candidates]} "
                        "(skipping merge, inserting as new sleeper row)\n"
                    )
                continue
            ourlads_row = candidates[0]
            old_pid = ourlads_row["player_id"]
            # Capture Ourlads-owned metadata to merge into the new row.
            merged_metadata[pid] = {
                "ourlads_id": ourlads_row["ourlads_id"],
                "depth_chart_position": ourlads_row["depth_chart_position"],
                "depth_chart_order": ourlads_row["depth_chart_order"],
                "depth_chart_last_observed_at": ourlads_row[
                    "depth_chart_last_observed_at"
                ],
            }
            # Insert a placeholder row at the new pid so the FK target exists
            # before we update note_player_mentions to point at it. Use a
            # minimal row that will be overwritten by the UPSERT below.
            self.conn.execute(
                "INSERT INTO players (player_id, full_name, team, position, "
                "updated_at, watchlist, source, ourlads_id, "
                "depth_chart_position, depth_chart_order, "
                "depth_chart_last_observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'merged', ?, ?, ?, ?) "
                "ON CONFLICT(player_id) DO NOTHING",
                (
                    pid,
                    full_name,
                    team,
                    position,
                    now,
                    ourlads_row["watchlist"],
                    ourlads_row["ourlads_id"],
                    ourlads_row["depth_chart_position"],
                    ourlads_row["depth_chart_order"],
                    ourlads_row["depth_chart_last_observed_at"],
                ),
            )
            # Move notes (polymorphic, no FK), mentions (FK with cascade),
            # and any feed_items bound to the merged-away pid — all to
            # point at the new player_id. The feed_items rewrite is
            # load-bearing: without it, the FK ON DELETE SET NULL on
            # feed_items.player_id silently drops the back-match the
            # moment a player gets merged.
            self.conn.execute(
                "UPDATE notes SET subject_id = ? "
                "WHERE subject_type = 'player' AND subject_id = ?",
                (pid, old_pid),
            )
            self.conn.execute(
                "UPDATE note_player_mentions SET player_id = ? WHERE player_id = ?",
                (pid, old_pid),
            )
            self.conn.execute(
                "UPDATE feed_items SET player_id = ? WHERE player_id = ?",
                (pid, old_pid),
            )
            # Now safe to delete the Ourlads-only row.
            self.conn.execute(
                "DELETE FROM players WHERE player_id = ?", (old_pid,)
            )
            # Tell the upsert below this row should be treated as a 'merged'
            # update (so it goes through the opt-out path that preserves
            # depth_chart values).
            existing_sources[pid] = "merged"

        # Per-row UPSERT (we need branching on existing source, so executemany
        # isn't ideal). DB has ~3k rows; per-row INSERT cost is microseconds.
        full_columns = list(PLAYER_FIELDS)
        full_placeholders = ", ".join("?" for _ in full_columns)
        full_update_cols = [f for f in full_columns if f != "player_id"]
        full_update_clause = ", ".join(
            f"{c} = excluded.{c}" for c in full_update_cols
        )
        full_upsert_sql = (
            f"INSERT INTO players ({', '.join(full_columns)}) "
            f"VALUES ({full_placeholders}) "
            f"ON CONFLICT(player_id) DO UPDATE SET {full_update_clause}"
        )
        # Ourlads-owned fields excluded from update set (still inserted on
        # first write). For an existing row with source='ourlads'/'merged',
        # the UPDATE branch keeps the row's existing depth_chart values.
        opt_out_update_cols = [
            f
            for f in full_columns
            if f not in ("player_id", "depth_chart_position", "depth_chart_order")
        ]
        opt_out_update_clause = ", ".join(
            f"{c} = excluded.{c}" for c in opt_out_update_cols
        )
        opt_out_upsert_sql = (
            f"INSERT INTO players ({', '.join(full_columns)}) "
            f"VALUES ({full_placeholders}) "
            f"ON CONFLICT(player_id) DO UPDATE SET {opt_out_update_clause}"
        )

        for row in validated:
            values = []
            for field in full_columns:
                if field == "player_id":
                    values.append(row["player_id"])
                elif field == "updated_at":
                    values.append(now)
                else:
                    values.append(row.get(field))
            existing = existing_sources.get(row["player_id"])
            if existing in ("ourlads", "merged"):
                # Per-field ownership: do not overwrite Ourlads-owned depth
                # chart on existing merged/ourlads rows. The INSERT branch
                # never fires here (row already exists), so only the UPDATE
                # set matters.
                self.conn.execute(opt_out_upsert_sql, tuple(values))
            else:
                # Either no existing row (insert with source='sleeper') or
                # existing source='sleeper' (full overwrite).
                self.conn.execute(full_upsert_sql, tuple(values))

        # Source-scoped DELETE: remove sleeper-source rows not in input.
        if seen_ids:
            marks = ",".join("?" for _ in seen_ids)
            params = tuple(seen_ids)
            self.conn.execute(
                f"DELETE FROM players WHERE source = 'sleeper' "
                f"AND player_id NOT IN ({marks})",
                params,
            )
        else:
            self.conn.execute(
                "DELETE FROM players WHERE source = 'sleeper'"
            )

        # Suffix-variant cleanup: collapse Ourlads-only rows whose
        # normalized name (with suffix-strip) now matches an existing
        # Sleeper or merged row on the same team+position. This catches
        # historical duplicates created when sources disagree on whether
        # to write "Marvin Harrison" vs "Marvin Harrison Jr". Runs after
        # the per-row UPSERT above so newly-written Sleeper rows are
        # eligible targets.
        self._merge_suffix_variant_duplicates()

        # Orphan-note cleanup: any player-subject note pointing at a player_id
        # no longer in the table (notes table is polymorphic, no FK cascade).
        self.conn.execute(
            "DELETE FROM notes WHERE subject_type = 'player' "
            "AND subject_id NOT IN (SELECT player_id FROM players)"
        )
        return len(validated)

    def _merge_suffix_variant_duplicates(self) -> int:
        """Per-sync sweep that collapses any rows whose normalized name +
        team + position collide.

        Two failure modes get cleaned up here:

        1. **Cross-source dupes** — the same player appears as a Sleeper
           (or merged) row AND an Ourlads-only row, because sources
           disagreed about a Jr/Sr/II/III/IV suffix at the time of
           identity-merge. The Sleeper / merged row wins (stable pid).

        2. **Intra-Ourlads dupes** — Ourlads occasionally lists the
           same player twice on its all-teams chart with different name
           variants (e.g. "Michael Penix Jr." at QB#1 and "Michael
           Penix" at QB#2). Picks a deterministic keeper and merges
           the rest into it.

        For each colliding bucket the keeper is chosen by:
          1. Prefer ``source IN ('sleeper','merged')`` over ``'ourlads'``
          2. Prefer the row with the lowest ``depth_chart_order`` (1 = starter)
          3. Tie-break by longest ``full_name`` (more complete metadata)
          4. Tie-break by lowest ``player_id`` (oldest / most stable)

        Notes, mentions, and feed_items pointing at the loser pids get
        rewritten to the keeper's pid; the duplicate rows are deleted.
        Returns the number of rows merged.
        """
        rows = self.conn.execute(
            "SELECT player_id, full_name, team, position, source, "
            "depth_chart_order FROM players"
        ).fetchall()
        if not rows:
            return 0

        buckets: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
        for r in rows:
            normalized = normalize_full_name(r["full_name"] or "")
            if not normalized:
                continue
            key = (r["team"] or "", r["position"] or "", normalized)
            buckets.setdefault(key, []).append(r)

        def keeper_sort_key(r: sqlite3.Row) -> tuple[int, int, int, str]:
            source_pref = 0 if r["source"] in ("sleeper", "merged") else 1
            order = (
                r["depth_chart_order"]
                if r["depth_chart_order"] is not None
                else 1_000_000
            )
            name_len_neg = -len(r["full_name"] or "")
            return (source_pref, order, name_len_neg, r["player_id"])

        merged = 0
        for group in buckets.values():
            if len(group) < 2:
                continue
            # Only collapse buckets where at least one row is
            # Ourlads-sourced — Sleeper IDs are authoritative, never
            # merge across two distinct stable Sleeper pids even if
            # they happen to normalize to the same key.
            if not any(r["source"] == "ourlads" for r in group):
                continue
            # Genuine ambiguity guard: if multiple non-Ourlads rows
            # exist in the same bucket, we cannot safely pick which
            # Sleeper player the Ourlads-only row(s) belong to. Leave
            # the bucket alone — the Ourlads rows stay as
            # ourlads-only, mirroring the existing ambiguous-match
            # posture of `find_player_for_match`.
            non_ourlads = [r for r in group if r["source"] != "ourlads"]
            if len(non_ourlads) > 1:
                continue
            sorted_group = sorted(group, key=keeper_sort_key)
            keeper = sorted_group[0]
            keeper_pid = keeper["player_id"]
            # Pull keeper's full row so we can upgrade fields with better
            # values from losers before deleting them.
            keeper_row = self.conn.execute(
                "SELECT * FROM players WHERE player_id = ?", (keeper_pid,)
            ).fetchone()
            best_name = keeper_row["full_name"] or ""
            best_dc_pos = keeper_row["depth_chart_position"]
            best_dc_order = (
                keeper_row["depth_chart_order"]
                if keeper_row["depth_chart_order"] is not None
                else 1_000_000
            )
            best_ourlads_id = keeper_row["ourlads_id"]
            best_dc_observed = keeper_row["depth_chart_last_observed_at"]
            absorbed_ourlads = False
            for loser in sorted_group[1:]:
                old_pid = loser["player_id"]
                # Transfer references first.
                self.conn.execute(
                    "UPDATE notes SET subject_id = ? "
                    "WHERE subject_type = 'player' AND subject_id = ?",
                    (keeper_pid, old_pid),
                )
                self.conn.execute(
                    "UPDATE note_player_mentions SET player_id = ? "
                    "WHERE player_id = ?",
                    (keeper_pid, old_pid),
                )
                self.conn.execute(
                    "UPDATE feed_items SET player_id = ? WHERE player_id = ?",
                    (keeper_pid, old_pid),
                )
                # Pull loser's full row so we can pick the better metadata.
                loser_row = self.conn.execute(
                    "SELECT * FROM players WHERE player_id = ?", (old_pid,)
                ).fetchone()
                if loser_row is not None:
                    if loser_row["source"] == "ourlads":
                        absorbed_ourlads = True
                    # Prefer the longer full_name (preserves Jr/Sr/II/III/IV).
                    loser_name = loser_row["full_name"] or ""
                    if len(loser_name) > len(best_name):
                        best_name = loser_name
                    # Prefer lower depth_chart_order (1=starter beats 2=backup).
                    loser_order = (
                        loser_row["depth_chart_order"]
                        if loser_row["depth_chart_order"] is not None
                        else 1_000_000
                    )
                    if loser_order < best_dc_order:
                        best_dc_order = loser_order
                        best_dc_pos = loser_row["depth_chart_position"]
                        best_dc_observed = loser_row[
                            "depth_chart_last_observed_at"
                        ]
                    # Inherit ourlads_id if keeper doesn't have one.
                    if best_ourlads_id is None and loser_row["ourlads_id"]:
                        best_ourlads_id = loser_row["ourlads_id"]
                self.conn.execute(
                    "DELETE FROM players WHERE player_id = ?", (old_pid,)
                )
                merged += 1
            # Apply the upgrades to the keeper.
            new_source = keeper_row["source"]
            if absorbed_ourlads and new_source == "sleeper":
                new_source = "merged"
            self.conn.execute(
                "UPDATE players SET full_name = ?, depth_chart_position = ?, "
                "depth_chart_order = ?, ourlads_id = ?, "
                "depth_chart_last_observed_at = ?, source = ? "
                "WHERE player_id = ?",
                (
                    best_name,
                    best_dc_pos,
                    best_dc_order if best_dc_order != 1_000_000 else None,
                    best_ourlads_id,
                    best_dc_observed,
                    new_source,
                    keeper_pid,
                ),
            )
        return merged

    def _upsert_ourlads_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        completeness: dict[str, bool] | None,
        run_start_at: str,
    ) -> int:
        written = 0
        for row in rows:
            team = row.get("team")
            full_name = row.get("full_name")
            position = row.get("position")
            if not (team and full_name and position):
                # Skip malformed rows defensively; Unit 4 should not produce these.
                continue
            ourlads_id = row.get("ourlads_id")
            jersey = row.get("number")
            normalized = normalize_full_name(full_name)

            existing_row: dict[str, Any] | None = None

            # Phase 1: ourlads_id lookup (if present and previously bound).
            if ourlads_id:
                hit = self.conn.execute(
                    "SELECT * FROM players WHERE ourlads_id = ?",
                    (ourlads_id,),
                ).fetchone()
                if hit is not None:
                    existing_row = _player_row(hit)

            # Phase 2: name+team+position lookup if no ourlads_id match.
            if existing_row is None:
                candidates = self.find_player_for_match(normalized, team, position)
                if len(candidates) == 1:
                    existing_row = candidates[0]
                elif len(candidates) > 1:
                    sys.stderr.write(
                        "ourlads:identity:ambiguous: "
                        f"name={normalized} team={team} position={position} "
                        f"candidates={[c['player_id'] for c in candidates]}\n"
                    )
                    existing_row = None  # fall through to insert as Ourlads-only

            if existing_row is not None:
                pid = existing_row["player_id"]
                new_source = "merged" if existing_row["source"] == "sleeper" else existing_row["source"]
                # Update in place: bind ourlads_id, set depth fields (if any),
                # bump source to 'merged' if existing was 'sleeper'. Don't
                # overwrite Sleeper-owned bio fields (first_name, last_name,
                # birth_date, etc.) — only update what Ourlads observed.
                update_fields = ["source = ?", "ourlads_id = COALESCE(?, ourlads_id)"]
                update_values: list[Any] = [new_source, ourlads_id]
                if "team" in row and row.get("team") is not None:
                    update_fields.append("team = ?")
                    update_values.append(row["team"])
                if "position" in row and row.get("position") is not None:
                    update_fields.append("position = ?")
                    update_values.append(row["position"])
                if jersey is not None:
                    update_fields.append("number = ?")
                    update_values.append(jersey)
                depth_pos = row.get("depth_chart_position")
                depth_order = row.get("depth_chart_order")
                wrote_depth = False
                if depth_pos is not None or depth_order is not None:
                    update_fields.append("depth_chart_position = ?")
                    update_values.append(depth_pos)
                    update_fields.append("depth_chart_order = ?")
                    update_values.append(depth_order)
                    update_fields.append("depth_chart_last_observed_at = ?")
                    update_values.append(run_start_at)
                    wrote_depth = True
                update_fields.append("updated_at = ?")
                update_values.append(_now())
                update_values.append(pid)
                self.conn.execute(
                    f"UPDATE players SET {', '.join(update_fields)} WHERE player_id = ?",
                    tuple(update_values),
                )
                # If we didn't write depth this row but the row had depth
                # fields set previously and Ourlads still saw the player,
                # bump last_observed_at so R13 doesn't sweep them.
                if not wrote_depth:
                    self.conn.execute(
                        "UPDATE players SET depth_chart_last_observed_at = ? "
                        "WHERE player_id = ? AND depth_chart_position IS NOT NULL",
                        (run_start_at, pid),
                    )
            else:
                # Insert as Ourlads-only.
                pid = (
                    f"ourlads:{ourlads_id}"
                    if ourlads_id
                    else synthesize_ourlads_id(team, jersey, normalized)
                )
                self.conn.execute(
                    "INSERT INTO players (player_id, full_name, team, position, "
                    "number, depth_chart_position, depth_chart_order, updated_at, "
                    "watchlist, source, ourlads_id, depth_chart_last_observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'ourlads', ?, ?) "
                    "ON CONFLICT(player_id) DO NOTHING",
                    (
                        pid,
                        full_name,
                        team,
                        position,
                        jersey,
                        row.get("depth_chart_position"),
                        row.get("depth_chart_order"),
                        _now(),
                        ourlads_id,
                        run_start_at if row.get("depth_chart_position") else None,
                    ),
                )
            written += 1

        # R13 conditional clear + source demotion. For each fully-observed
        # team, any source IN ('ourlads','merged') row on that team whose
        # depth_chart_last_observed_at is older than this run's start has
        # its depth fields cleared. Merged rows additionally demote back to
        # 'sleeper' (clear ourlads_id) so Sleeper resumes ownership.
        if completeness:
            for team_abbr, was_complete in completeness.items():
                if not was_complete:
                    continue
                # Demote merged rows that fell off the chart.
                self.conn.execute(
                    "UPDATE players SET source = 'sleeper', ourlads_id = NULL, "
                    "depth_chart_position = NULL, depth_chart_order = NULL "
                    "WHERE team = ? AND source = 'merged' AND ("
                    "depth_chart_last_observed_at IS NULL "
                    "OR depth_chart_last_observed_at < ?)",
                    (team_abbr, run_start_at),
                )
                # Clear depth fields on Ourlads-only rows that fell off (but
                # leave source='ourlads' — they have no Sleeper presence).
                self.conn.execute(
                    "UPDATE players SET depth_chart_position = NULL, "
                    "depth_chart_order = NULL "
                    "WHERE team = ? AND source = 'ourlads' AND ("
                    "depth_chart_last_observed_at IS NULL "
                    "OR depth_chart_last_observed_at < ?)",
                    (team_abbr, run_start_at),
                )

        # Tail-of-sync dupe collapse — handles the intra-Ourlads case
        # where the same player appeared on the chart twice with
        # different name variants (e.g. "Michael Penix Jr." at QB#1 and
        # "Michael Penix" at QB#2). Same sweep also fires at the tail
        # of Sleeper sync; running either source will clean up dupes.
        self._merge_suffix_variant_duplicates()
        return written

    def get_player(self, player_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM players WHERE player_id = ?", (str(player_id),)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Player {player_id!r} not found")
        return _player_row(row)

    def list_players(
        self,
        team: str | None = None,
        position: str | None = None,
        watchlist: bool | None = None,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if team:
            clauses.append("team = ? COLLATE NOCASE")
            params.append(team)
        if position:
            clauses.append("position = ? COLLATE NOCASE")
            params.append(position)
        if watchlist is not None:
            clauses.append("watchlist = ?")
            params.append(1 if watchlist else 0)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM players {where} ORDER BY full_name COLLATE NOCASE",
            tuple(params),
        ).fetchall()
        return [_player_row(r) for r in rows]

    def set_watchlist(self, player_id: str, on: bool) -> dict[str, Any]:
        self.get_player(player_id)
        self.conn.execute(
            "UPDATE players SET watchlist = ? WHERE player_id = ?",
            (1 if on else 0, str(player_id)),
        )
        self.conn.commit()
        return self.get_player(player_id)

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

    def _resolve_mentions(
        self, mentions: dict[str, Any] | None
    ) -> tuple[list[str], list[str]]:
        """Return (player_ids, team_abbrs) lists, validated and deduped.

        Raises NotFoundError on unknown player_id or unknown team identifier.
        Raises AmbiguousTeamError on ambiguous team identifier.
        """
        if not mentions:
            return [], []
        raw_player_ids = mentions.get("player_ids") or []
        raw_team_ids = mentions.get("team_abbrs") or []

        # Dedupe while preserving caller order.
        seen: set[str] = set()
        player_ids: list[str] = []
        for pid in raw_player_ids:
            spid = str(pid)
            if spid not in seen:
                seen.add(spid)
                player_ids.append(spid)

        if player_ids:
            marks = ",".join("?" for _ in player_ids)
            rows = self.conn.execute(
                f"SELECT player_id FROM players WHERE player_id IN ({marks})",
                tuple(player_ids),
            ).fetchall()
            found = {r["player_id"] for r in rows}
            missing = [p for p in player_ids if p not in found]
            if missing:
                raise NotFoundError(f"Unknown mentioned player_id(s): {missing}")

        team_abbrs: list[str] = []
        seen_abbrs: set[str] = set()
        for ident in raw_team_ids:
            team = self.get_team(ident)  # may raise NotFound / Ambiguous
            if team["abbr"] not in seen_abbrs:
                seen_abbrs.add(team["abbr"])
                team_abbrs.append(team["abbr"])

        return player_ids, team_abbrs

    def _write_mentions(
        self, note_id: int, player_ids: list[str], team_abbrs: list[str]
    ) -> None:
        if player_ids:
            self.conn.executemany(
                "INSERT INTO note_player_mentions (note_id, player_id) VALUES (?, ?)",
                [(note_id, pid) for pid in player_ids],
            )
        if team_abbrs:
            self.conn.executemany(
                "INSERT INTO note_team_mentions (note_id, team_abbr) VALUES (?, ?)",
                [(note_id, abbr) for abbr in team_abbrs],
            )

    def _load_mentions_for(
        self, note_ids: list[int]
    ) -> dict[int, dict[str, list[dict[str, Any]]]]:
        """Batched mention load. Returns {note_id: {"players": [...], "teams": [...]}}."""
        if not note_ids:
            return {}
        out: dict[int, dict[str, list[dict[str, Any]]]] = {
            nid: {"players": [], "teams": []} for nid in note_ids
        }
        marks = ",".join("?" for _ in note_ids)

        prows = self.conn.execute(
            f"SELECT npm.note_id, p.player_id, p.full_name, p.team, p.position "
            f"FROM note_player_mentions npm "
            f"JOIN players p ON p.player_id = npm.player_id "
            f"WHERE npm.note_id IN ({marks}) "
            f"ORDER BY p.full_name COLLATE NOCASE",
            tuple(note_ids),
        ).fetchall()
        for r in prows:
            out[r["note_id"]]["players"].append(
                {
                    "player_id": r["player_id"],
                    "full_name": r["full_name"],
                    "team": r["team"],
                    "position": r["position"],
                }
            )

        trows = self.conn.execute(
            f"SELECT ntm.note_id, t.abbr, t.full_name "
            f"FROM note_team_mentions ntm "
            f"JOIN teams t ON t.abbr = ntm.team_abbr "
            f"WHERE ntm.note_id IN ({marks}) "
            f"ORDER BY t.full_name COLLATE NOCASE",
            tuple(note_ids),
        ).fetchall()
        for r in trows:
            out[r["note_id"]]["teams"].append(
                {"abbr": r["abbr"], "full_name": r["full_name"]}
            )
        return out

    def _attach_mentions(self, notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not notes:
            return notes
        loaded = self._load_mentions_for([n["id"] for n in notes])
        for n in notes:
            n["mentions"] = loaded.get(n["id"], {"players": [], "teams": []})
        return notes

    def _add_note(
        self,
        subject_type: str,
        subject_id: str,
        body: str,
        mentions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        player_ids, team_abbrs = self._resolve_mentions(mentions)
        now = _now()
        try:
            self.conn.execute("BEGIN")
            cur = self.conn.execute(
                "INSERT INTO notes (subject_type, subject_id, body, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (subject_type, subject_id, body, now, now),
            )
            note_id = int(cur.lastrowid)
            self._write_mentions(note_id, player_ids, team_abbrs)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        row = self.conn.execute(
            "SELECT * FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        note = _note_row(row)
        note["mentions"] = self._load_mentions_for([note_id])[note_id]
        return note

    def _list_notes(self, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM notes WHERE subject_type = ? AND subject_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (subject_type, subject_id),
        ).fetchall()
        return self._attach_mentions([_note_row(r) for r in rows])

    def _list_mention_notes(
        self, target_type: str, target_id: str
    ) -> list[dict[str, Any]]:
        """Notes that mention the given player or team but are NOT primarily about it."""
        if target_type == "player":
            rows = self.conn.execute(
                "SELECT n.* FROM notes n "
                "JOIN note_player_mentions m ON m.note_id = n.id "
                "WHERE m.player_id = ? "
                "  AND NOT (n.subject_type = 'player' AND n.subject_id = ?) "
                "ORDER BY n.created_at DESC, n.id DESC",
                (target_id, target_id),
            ).fetchall()
        elif target_type == "team":
            rows = self.conn.execute(
                "SELECT n.* FROM notes n "
                "JOIN note_team_mentions m ON m.note_id = n.id "
                "WHERE m.team_abbr = ? "
                "  AND NOT (n.subject_type = 'team' AND n.subject_id = ?) "
                "ORDER BY n.created_at DESC, n.id DESC",
                (target_id, target_id),
            ).fetchall()
        else:
            return []
        return self._attach_mentions([_note_row(r) for r in rows])

    def add_note(
        self, player_id: str, body: str, mentions: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.get_player(player_id)
        return self._add_note("player", str(player_id), body, mentions)

    def list_notes(self, player_id: str) -> list[dict[str, Any]]:
        self.get_player(player_id)
        return self._list_notes("player", str(player_id))

    def list_player_mentions(self, player_id: str) -> list[dict[str, Any]]:
        self.get_player(player_id)
        return self._list_mention_notes("player", str(player_id))

    def add_team_note(
        self, team_identifier: str, body: str, mentions: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        team = self.get_team(team_identifier)
        return self._add_note("team", team["abbr"], body, mentions)

    def list_team_notes(self, team_identifier: str) -> list[dict[str, Any]]:
        team = self.get_team(team_identifier)
        return self._list_notes("team", team["abbr"])

    def list_team_mentions(self, team_identifier: str) -> list[dict[str, Any]]:
        team = self.get_team(team_identifier)
        return self._list_mention_notes("team", team["abbr"])

    # --- studies ---

    def create_study(
        self, title: str, description: str | None = None
    ) -> dict[str, Any]:
        if not title or not title.strip():
            raise ValueError("Study title is required")
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO studies (title, description, status, created_at, updated_at) "
            "VALUES (?, ?, 'open', ?, ?)",
            (title.strip(), description, now, now),
        )
        self.conn.commit()
        return self.get_study(int(cur.lastrowid))

    def get_study(self, study_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM studies WHERE id = ?", (int(study_id),)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Study {study_id} not found")
        return _study_row(row)

    def list_studies(self, status: str | None = "open") -> list[dict[str, Any]]:
        if status is None:
            rows = self.conn.execute(
                "SELECT * FROM studies ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        else:
            if status not in ("open", "archived"):
                raise ValueError(f"Invalid status: {status!r}")
            rows = self.conn.execute(
                "SELECT * FROM studies WHERE status = ? "
                "ORDER BY updated_at DESC, id DESC",
                (status,),
            ).fetchall()
        return [_study_row(r) for r in rows]

    def update_study(
        self,
        study_id: int,
        title: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        self.get_study(study_id)
        sets = []
        params: list[Any] = []
        if title is not None:
            if not title.strip():
                raise ValueError("Study title cannot be empty")
            sets.append("title = ?")
            params.append(title.strip())
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if not sets:
            return self.get_study(study_id)
        sets.append("updated_at = ?")
        params.append(_now())
        params.append(int(study_id))
        self.conn.execute(
            f"UPDATE studies SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        self.conn.commit()
        return self.get_study(study_id)

    def set_study_status(self, study_id: int, status: str) -> dict[str, Any]:
        if status not in ("open", "archived"):
            raise ValueError(f"Invalid status: {status!r}")
        self.get_study(study_id)
        self.conn.execute(
            "UPDATE studies SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), int(study_id)),
        )
        self.conn.commit()
        return self.get_study(study_id)

    def delete_study(self, study_id: int) -> None:
        self.get_study(study_id)
        try:
            self.conn.execute("BEGIN")
            self.conn.execute(
                "DELETE FROM notes WHERE subject_type = 'study' AND subject_id = ?",
                (str(study_id),),
            )
            self.conn.execute(
                "DELETE FROM studies WHERE id = ?", (int(study_id),)
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def add_study_note(
        self,
        study_id: int,
        body: str,
        mentions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.get_study(study_id)
        return self._add_note("study", str(study_id), body, mentions)

    def list_study_notes(self, study_id: int) -> list[dict[str, Any]]:
        self.get_study(study_id)
        return self._list_notes("study", str(study_id))

    # --- prompts ---

    def list_prompts(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT slug, title, description, body FROM prompts "
            "ORDER BY (CASE slug WHEN 'show-prompt-library' THEN 0 ELSE 1 END), slug"
        ).fetchall()
        return [
            {
                "slug": r["slug"],
                "title": r["title"],
                "description": r["description"],
                "body": r["body"],
            }
            for r in rows
        ]

    # --- recent notes feed ---

    def list_recent_notes(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return notes across all players and teams, newest first, with subject
        info resolved so callers can render a readable feed.
        """
        rows = self.conn.execute(
            "SELECT n.*, "
            "       p.full_name AS player_full_name, "
            "       p.team      AS player_team, "
            "       p.position  AS player_position, "
            "       t.full_name AS team_full_name, "
            "       s.title     AS study_title, "
            "       s.status    AS study_status "
            "FROM notes n "
            "LEFT JOIN players p "
            "       ON n.subject_type = 'player' AND n.subject_id = p.player_id "
            "LEFT JOIN teams t "
            "       ON n.subject_type = 'team'   AND n.subject_id = t.abbr "
            "LEFT JOIN studies s "
            "       ON n.subject_type = 'study'  AND n.subject_id = CAST(s.id AS TEXT) "
            "ORDER BY n.created_at DESC, n.id DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()

        out: list[dict[str, Any]] = []
        for r in rows:
            note = _note_row(r)
            if r["subject_type"] == "player":
                note["subject"] = {
                    "type": "player",
                    "player_id": r["subject_id"],
                    "full_name": r["player_full_name"],
                    "team": r["player_team"],
                    "position": r["player_position"],
                }
            elif r["subject_type"] == "team":
                note["subject"] = {
                    "type": "team",
                    "abbr": r["subject_id"],
                    "full_name": r["team_full_name"],
                }
            else:
                note["subject"] = {
                    "type": "study",
                    "study_id": int(r["subject_id"]),
                    "title": r["study_title"],
                    "status": r["study_status"],
                }
            out.append(note)
        return self._attach_mentions(out)

    def update_note(
        self,
        note_id: int,
        body: str,
        mentions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Resolve mentions before opening the transaction so a bad mention
        # doesn't leave a half-updated note.
        resolved: tuple[list[str], list[str]] | None = None
        if mentions is not None:
            resolved = self._resolve_mentions(mentions)

        try:
            self.conn.execute("BEGIN")
            cur = self.conn.execute(
                "UPDATE notes SET body = ?, updated_at = ? WHERE id = ?",
                (body, _now(), note_id),
            )
            if cur.rowcount == 0:
                self.conn.rollback()
                raise NotFoundError(f"Note {note_id} not found")
            if resolved is not None:
                player_ids, team_abbrs = resolved
                self.conn.execute(
                    "DELETE FROM note_player_mentions WHERE note_id = ?", (note_id,)
                )
                self.conn.execute(
                    "DELETE FROM note_team_mentions WHERE note_id = ?", (note_id,)
                )
                self._write_mentions(note_id, player_ids, team_abbrs)
            self.conn.commit()
        except NotFoundError:
            raise
        except Exception:
            self.conn.rollback()
            raise
        row = self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        note = _note_row(row)
        note["mentions"] = self._load_mentions_for([note_id])[note_id]
        return note

    def delete_note(self, note_id: int) -> None:
        cur = self.conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self.conn.commit()
        if cur.rowcount == 0:
            raise NotFoundError(f"Note {note_id} not found")

    # --- sync runs ---

    def record_sync_start(self, source_url: str, source: str = "sleeper") -> int:
        """Record the start of a sync run. Raises ConcurrentSyncError if another
        run is already in flight (status='running' with started_at within the
        last 5 minutes — older runs are treated as crashed and ignored).
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat(timespec="seconds")
        existing = self.conn.execute(
            "SELECT id, source FROM sync_runs WHERE status = 'running' "
            "AND started_at > ?",
            (cutoff,),
        ).fetchone()
        if existing is not None:
            raise ConcurrentSyncError(
                f"Another sync ({existing['source']}, run_id={existing['id']}) "
                "is already running. Wait for it to finish or fail."
            )
        cur = self.conn.execute(
            "INSERT INTO sync_runs (started_at, source_url, status, source) "
            "VALUES (?, ?, 'running', ?)",
            (_now(), source_url, source),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_sync_finish(
        self,
        run_id: int,
        players_written: int | None = None,
        status: str = "success",
        error: str | None = None,
        *,
        items_fetched: int | None = None,
        items_new: int | None = None,
        items_matched: int | None = None,
        items_unmatched: int | None = None,
    ) -> dict[str, Any]:
        """Mark a sync run finished. `players_written` is set for player-data
        syncs (sleeper, ourlads); the four `items_*` kwargs are set for feed
        syncs. The other set is left NULL.
        """
        self.conn.execute(
            "UPDATE sync_runs SET finished_at = ?, players_written = ?, "
            "status = ?, error = ?, "
            "items_fetched = ?, items_new = ?, items_matched = ?, items_unmatched = ? "
            "WHERE id = ?",
            (
                _now(),
                players_written,
                status,
                error,
                items_fetched,
                items_new,
                items_matched,
                items_unmatched,
                run_id,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM sync_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"sync_run {run_id} not found")
        return _sync_run_row(row)

    def get_sync_run(self, run_id: int) -> dict[str, Any] | None:
        """Return a single sync_runs row by id, or None if not found."""
        row = self.conn.execute(
            "SELECT * FROM sync_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return _sync_run_row(row) if row else None

    def last_sync(self, source: str | None = None) -> dict[str, Any] | None:
        """Return the most recent sync run. If `source` is given, restrict to
        runs of that source (e.g. 'sleeper' or 'ourlads').
        """
        if source is None:
            row = self.conn.execute(
                "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM sync_runs WHERE source = ? ORDER BY id DESC LIMIT 1",
                (source,),
            ).fetchone()
        return _sync_run_row(row) if row else None

    # --- feeds ---

    def _feed_source_id(self, name: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM feed_sources WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            # Lazy re-seed: an adapter may have been registered after this
            # Database was opened (test scenarios, hot-reloaded modules).
            self._seed_feed_sources()
            row = self.conn.execute(
                "SELECT id FROM feed_sources WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"feed source {name!r} not registered")
        return int(row["id"])

    def feed_item_exists(self, source_name: str, external_id: str) -> bool:
        """Return True if a feed_items row already exists for this
        (source, external_id). Used by the orchestrator's `is_seen` callback
        to drive the incremental-stop loop.
        """
        source_id = self._feed_source_id(source_name)
        row = self.conn.execute(
            "SELECT 1 FROM feed_items WHERE source_id = ? AND external_id = ? LIMIT 1",
            (source_id, external_id),
        ).fetchone()
        return row is not None

    def add_feed_item_with_auto_note(
        self,
        source_name: str,
        item: dict[str, Any],
        *,
        player_id: str | None,
        note_body: str | None = None,
        run_id: int | None = None,
    ) -> dict[str, Any]:
        """Single-transaction upsert + optional auto-note creation.

        On a fresh `(source, external_id)`, inserts the feed_items row, and
        if `player_id` is given AND `note_body` is provided, also inserts a
        `notes` row + `note_player_mentions` link and stamps the feed_items
        row with `note_id` and `note_run_id` — all atomically.

        On a re-seen `(source, external_id)` whose existing row is unmatched
        (`player_id IS NULL`), this method will back-match: update player_id
        and create the auto-note in the same transaction.

        Returns a dict: {feed_item_id, was_new, note_id, matched_now}. The
        `was_new` flag means the feed_items row didn't exist; `matched_now`
        means this call set or updated the player_id (true on first match
        OR on back-match upgrade).
        """
        source_id = self._feed_source_id(source_name)
        external_id = str(item["external_id"])
        now = _now()

        try:
            self.conn.execute("BEGIN")

            existing = self.conn.execute(
                "SELECT id, player_id, note_id FROM feed_items "
                "WHERE source_id = ? AND external_id = ?",
                (source_id, external_id),
            ).fetchone()

            if existing is None:
                cur = self.conn.execute(
                    "INSERT INTO feed_items ("
                    "  source_id, external_id, external_player_id,"
                    "  external_player_name, external_team, external_position,"
                    "  team_abbr, source_url, source_author, raw_html,"
                    "  cleaned_text, created_at, ingested_at, player_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        source_id,
                        external_id,
                        item.get("external_player_id"),
                        item["external_player_name"],
                        item.get("external_team"),
                        item.get("external_position"),
                        item.get("team_abbr"),
                        item.get("source_url"),
                        item.get("source_author"),
                        item.get("raw_html"),
                        item["cleaned_text"],
                        item["created_at"],
                        now,
                        player_id,
                    ),
                )
                feed_item_id = int(cur.lastrowid)
                was_new = True
                matched_now = player_id is not None
                already_has_note = False
            else:
                feed_item_id = int(existing["id"])
                was_new = False
                already_has_note = existing["note_id"] is not None
                # Back-match: only fill in player_id if it was previously NULL.
                if (
                    existing["player_id"] is None
                    and player_id is not None
                ):
                    self.conn.execute(
                        "UPDATE feed_items SET player_id = ? WHERE id = ?",
                        (player_id, feed_item_id),
                    )
                    matched_now = True
                else:
                    matched_now = False

            note_id: int | None = None
            should_write_note = (
                matched_now
                and player_id is not None
                and note_body is not None
                and not already_has_note
            )
            if should_write_note:
                note_cur = self.conn.execute(
                    "INSERT INTO notes (subject_type, subject_id, body, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("player", str(player_id), note_body, now, now),
                )
                note_id = int(note_cur.lastrowid)
                self.conn.execute(
                    "INSERT INTO note_player_mentions (note_id, player_id) VALUES (?, ?)",
                    (note_id, player_id),
                )
                self.conn.execute(
                    "UPDATE feed_items SET note_id = ?, note_run_id = ? WHERE id = ?",
                    (note_id, run_id, feed_item_id),
                )

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return {
            "feed_item_id": feed_item_id,
            "was_new": was_new,
            "matched_now": matched_now,
            "note_id": note_id,
        }

    def list_feed_items(
        self,
        *,
        player_id: str | None = None,
        source: str | None = None,
        since: str | None = None,
        matched: bool | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Read access to the raw feed. Filters are AND-combined."""
        clauses: list[str] = []
        params: list[Any] = []
        if player_id is not None:
            clauses.append("fi.player_id = ?")
            params.append(str(player_id))
        if source is not None:
            clauses.append("fs.name = ?")
            params.append(source)
        if since is not None:
            clauses.append("fi.created_at >= ?")
            params.append(since)
        if matched is True:
            clauses.append("fi.player_id IS NOT NULL")
        elif matched is False:
            clauses.append("fi.player_id IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT fi.*, fs.name AS source_name FROM feed_items fi "
            f"JOIN feed_sources fs ON fs.id = fi.source_id "
            f"{where} "
            f"ORDER BY fi.created_at DESC, fi.id DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        return [_feed_item_row(r, source_name=r["source_name"]) for r in rows]

    def find_unmatched_feed_items_since(
        self, *, window_days: int = 30
    ) -> list[dict[str, Any]]:
        """Return unmatched feed_items ingested within the window. Used by
        the back-match pass. Only includes the fields needed to retry a match.
        """
        rows = self.conn.execute(
            "SELECT id, source_id, external_player_name, team_abbr, "
            "external_position FROM feed_items "
            "WHERE player_id IS NULL "
            "AND ingested_at > datetime('now', ?) "
            "ORDER BY id ASC",
            (f"-{int(window_days)} days",),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "source_id": int(r["source_id"]),
                "external_player_name": r["external_player_name"],
                "team_abbr": r["team_abbr"],
                "external_position": r["external_position"],
            }
            for r in rows
        ]

    def rematch_recent_unmatched_feed_items(
        self,
        *,
        window_days: int = 30,
        run_id: int | None = None,
        note_body_for: Any = None,  # callable(item_dict) -> str | None
    ) -> dict[str, int]:
        """Retry identity match for recent unmatched feed_items.

        For each row, runs ``find_player_for_match(normalize_full_name(name),
        team_abbr, position)`` and on a single-match attaches the player and
        writes the auto-note (single transaction per item via
        ``add_feed_item_with_auto_note``'s back-match path). Returns
        ``{checked, matched, notes_written}``.

        ``note_body_for`` is an optional callable that takes the loaded
        feed_items row dict and returns a note body string. If omitted, no
        notes are written even when matches are found (data is still
        attached). The orchestrator passes a closure that builds the body
        from the full feed_items record.
        """
        candidates = self.find_unmatched_feed_items_since(window_days=window_days)
        checked = 0
        matched = 0
        notes_written = 0
        for cand in candidates:
            checked += 1
            team = cand["team_abbr"]
            pos = cand["external_position"]
            name = cand["external_player_name"]
            if not (team and pos and name):
                continue
            normalized = normalize_full_name(name)
            players = self.find_player_for_match(normalized, team, pos)
            if len(players) != 1:
                continue
            player_id = players[0]["player_id"]
            # Load the full feed_items row so we can rebuild the note body.
            full = self.conn.execute(
                "SELECT fi.*, fs.name AS source_name FROM feed_items fi "
                "JOIN feed_sources fs ON fs.id = fi.source_id "
                "WHERE fi.id = ?",
                (cand["id"],),
            ).fetchone()
            if full is None:
                continue
            item_record = _feed_item_row(full, source_name=full["source_name"])
            body: str | None = None
            if note_body_for is not None:
                try:
                    body = note_body_for(item_record)
                except Exception:
                    body = None
            result = self.add_feed_item_with_auto_note(
                full["source_name"],
                {
                    "external_id": full["external_id"],
                    "external_player_id": full["external_player_id"],
                    "external_player_name": full["external_player_name"],
                    "external_team": full["external_team"],
                    "external_position": full["external_position"],
                    "team_abbr": full["team_abbr"],
                    "source_url": full["source_url"],
                    "source_author": full["source_author"],
                    "raw_html": full["raw_html"],
                    "cleaned_text": full["cleaned_text"],
                    "created_at": full["created_at"],
                },
                player_id=player_id,
                note_body=body,
                run_id=run_id,
            )
            if result["matched_now"]:
                matched += 1
            if result["note_id"] is not None:
                notes_written += 1
        return {"checked": checked, "matched": matched, "notes_written": notes_written}

    def delete_feed_item(self, feed_item_id: int) -> None:
        """Application-level cascade: delete the feed_items row and any
        linked auto-note in one transaction. This is the only supported
        delete path for feed_items rows because SQLite cannot enforce the
        feed_items -> notes cascade with FKs.
        """
        try:
            self.conn.execute("BEGIN")
            row = self.conn.execute(
                "SELECT note_id FROM feed_items WHERE id = ?", (int(feed_item_id),)
            ).fetchone()
            if row is None:
                self.conn.rollback()
                raise NotFoundError(f"feed_item {feed_item_id} not found")
            note_id = row["note_id"]
            self.conn.execute(
                "DELETE FROM feed_items WHERE id = ?", (int(feed_item_id),)
            )
            if note_id is not None:
                self.conn.execute("DELETE FROM notes WHERE id = ?", (int(note_id),))
            self.conn.commit()
        except NotFoundError:
            raise
        except Exception:
            self.conn.rollback()
            raise

    def delete_auto_notes_from_run(self, run_id: int) -> int:
        """Bulk-rollback for a misfiring feed sync. Deletes every auto-note
        whose backing feed_items row was first-attached during the given
        sync run. Leaves the raw feed_items rows alive (their `note_id`
        gets set to NULL via the FK ON DELETE SET NULL). Idempotent.
        Non-feed run_ids are no-ops because they never set note_run_id.
        """
        # Collect note_ids first so we can both delete and report a count.
        note_ids = [
            int(r["note_id"])
            for r in self.conn.execute(
                "SELECT note_id FROM feed_items "
                "WHERE note_run_id = ? AND note_id IS NOT NULL",
                (int(run_id),),
            ).fetchall()
        ]
        if not note_ids:
            return 0
        marks = ",".join("?" for _ in note_ids)
        self.conn.execute(
            f"DELETE FROM notes WHERE id IN ({marks})", tuple(note_ids)
        )
        self.conn.commit()
        return len(note_ids)

    def close(self) -> None:
        self.conn.close()
