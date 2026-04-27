---
date: 2026-04-26
topic: studies-and-mentions
---

# Studies and Mentions

## Problem Frame

Notes today are tied to a single player or team. Two real workflows aren't supported:

1. **Long-running research.** When the user is investigating a question like "which RB handcuffs are worth a late pick?", they accumulate findings over days. The notes don't naturally belong to one player or team — they belong to the *question*. Today those notes have nowhere to live.
2. **Cross-references.** A note about Pacheco vs Hunt at KC mentions three things (Pacheco, Hunt, KC), but only attaches to one. From any of the other entities, the note is invisible.

This adds **studies** (named research containers) as a third note subject, and **mentions** (an N-to-many tag list on every note) so cross-references are first-class.

## Requirements

**Studies (new subject type)**

- R1. A `study` has a title, optional description, and status (`open` or `archived`). Notes attach to a study the same way they attach to a player or team.
- R2. Tools exist to create, list, get, update, archive/unarchive, and delete studies.
- R3. `list_studies` defaults to status `open`. An optional argument lets the user include archived ones (or list only archived).
- R4. `get_study(study_id)` returns the study record and its notes (newest first).
- R5. Deleting a study deletes its notes (cascade). Archiving is the way to keep the notes around without cluttering the active list.

**Mentions on notes**

- R6. Every note (regardless of subject — player, team, or study) can carry a list of mentioned players and a list of mentioned teams. The mentions are passed explicitly when the note is created or updated; the system does not parse them out of the note body.
- R7. Mentions are validated at write time: every mentioned player must exist in `players`, and every mentioned team must resolve via the same identifier rules used by `get_depth_chart` (abbr / full name / nickname). Invalid mentions raise a clear error and the note is not written.
- R8. Mentions are included in note payloads returned by every list/get tool, so the agent can render them without follow-up calls.

**Player and team views**

- R9. `get_player(player_id)` returns two distinct lists: `notes` (where this player is the primary subject) and `mentions` (notes about something else that tag this player). Both newest first.
- R10. `get_team(team)` mirrors the same shape: `notes` for primary-subject team notes and `mentions` for notes elsewhere that tag this team. (Today `get_team` doesn't exist as a tool — `list_team_notes` does — so this requirement also implies a `get_team` tool that returns the team record alongside both lists.)
- R11. Existing `list_notes(player_id)` and `list_team_notes(team)` continue to return only primary-subject notes, unchanged. The dual-list view lives in `get_player` / `get_team`.

**Cross-cutting feed**

- R12. The existing `list_recent_notes` tool continues to work, now also covering study notes, with the subject block correctly identifying studies. Mentions are included on each entry.

## Success Criteria

- The user can say "start a study on RB handcuffs" and Claude creates one, then attaches notes to it over multiple sessions.
- A note like "Pacheco vs Hunt for KC's RB1 job" can be written once and is discoverable from Pacheco, Hunt, and KC's views without duplicating the note.
- The "open studies" list stays focused on what's currently active — archived studies don't pollute it.
- Existing player and team note workflows still work; nothing the user already does breaks.

## Scope Boundaries

- No automatic mention detection from note body text. Mentions are explicit.
- No nesting (a study can't contain another study; notes live one level under a subject).
- No multi-subject notes (a note has exactly one primary subject — player, team, or study). Cross-references happen via mentions.
- No collaboration, sharing, or multi-user concerns. Single-user local DB.
- No search across study titles/bodies in this iteration. `list_studies` is enough.
- No tagging of notes with arbitrary string labels. Mentions are typed (player or team) only.

## Key Decisions

- **Studies are a third polymorphic subject_type, not a parallel concept.** Rationale: notes are already polymorphic (player/team); adding `study` keeps the model uniform and lets `list_recent_notes` and any future feed work without special cases.
- **Mentions are explicit, supplied by the agent.** Rationale: Claude already knows who the note is about when it writes the note; explicit mentions sidestep all the brittle name-matching edge cases (partial names, team-abbr-vs-word collisions, nicknames).
- **Player view shows mentions in a separate list, not merged.** Rationale: the agent and the user both want to know whether a note is *about* a player or *mentions* them — the distinction matters when scanning.
- **Status (open/archived), not free-form labels, gates study visibility.** Rationale: the failure mode we care about is old studies cluttering the active list. A 2-state flag solves that with no taxonomy maintenance.

## Dependencies / Assumptions

- The existing polymorphic `notes` table (introduced last iteration) is the foundation. Extending `subject_type` to include `study` is straightforward.
- Mentions are stored separately (an N-to-many relation between notes and players/teams). Exact table shape is a planning concern.

## Outstanding Questions

### Resolve Before Planning

_(none — all blocking product decisions are resolved.)_

### Deferred to Planning

- [Affects R6, R7][Technical] How exactly do mentions get serialized in tool inputs and outputs (e.g., parallel arrays vs an objects list)? Pick a shape during planning.
- [Affects R6, R7][Technical] How do `update_note` and `update_team_note` interact with mentions? (Replace the full mention set on update? Add/remove deltas? Likely "replace whole list" for simplicity — confirm during planning.)
- [Affects R10][Technical] `get_team` is being introduced as a new tool — confirm its full payload shape during planning (likely: team record + primary notes + mention notes, mirroring `get_player`).
- [Affects R2][Technical] Whether `add_note` (player), `add_team_note`, and `add_study_note` should be three typed tools or unified into a single `add_note(subject_type, subject_id, body, mentions?)`. Both work; pick during planning based on agent-call ergonomics.

## Next Steps

→ `/ce:plan` for structured implementation planning
