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


SCHEMA_VERSION = 7


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
  source TEXT NOT NULL DEFAULT 'sleeper'
);

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
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "players_written": row["players_written"],
        "source_url": row["source_url"],
        "status": row["status"],
        "error": row["error"],
        "source": row["source"],
    }


class Database:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        self._seed_teams()
        self._seed_prompts()

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
            # Drop only players absent from the new set; explicitly delete their
            # notes (notes table no longer has an FK cascade since it is polymorphic).
            if seen_ids:
                marks = ",".join("?" for _ in seen_ids)
                params = tuple(seen_ids)
                self.conn.execute(
                    f"DELETE FROM notes WHERE subject_type = 'player' "
                    f"AND subject_id NOT IN ({marks})",
                    params,
                )
                self.conn.execute(
                    f"DELETE FROM players WHERE player_id NOT IN ({marks})",
                    params,
                )
            else:
                self.conn.execute(
                    "DELETE FROM notes WHERE subject_type = 'player'"
                )
                self.conn.execute("DELETE FROM players")
            if prepared:
                self.conn.executemany(upsert_sql, prepared)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(prepared)

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

        # Orphan-note cleanup: any player-subject note pointing at a player_id
        # no longer in the table (notes table is polymorphic, no FK cascade).
        self.conn.execute(
            "DELETE FROM notes WHERE subject_type = 'player' "
            "AND subject_id NOT IN (SELECT player_id FROM players)"
        )
        return len(validated)

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

    def close(self) -> None:
        self.conn.close()
