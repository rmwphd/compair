-- CompAIR schema. Applied idempotently at startup.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS surveys (
    id                    INTEGER PRIMARY KEY,
    slug                  TEXT NOT NULL UNIQUE,
    title                 TEXT NOT NULL,
    prompt                TEXT NOT NULL DEFAULT 'Which do you prefer?',
    intro_md              TEXT NOT NULL DEFAULT '',
    consent_md            TEXT NOT NULL DEFAULT '',
    pairs_per_participant INTEGER,              -- NULL = unlimited
    allow_tie             INTEGER NOT NULL DEFAULT 1,
    allow_skip            INTEGER NOT NULL DEFAULT 1,
    status                TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','open','closed')),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    survey_id   INTEGER NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_items_survey ON items(survey_id, active);

CREATE TABLE IF NOT EXISTS demo_questions (
    id           INTEGER PRIMARY KEY,
    survey_id    INTEGER NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
    key          TEXT NOT NULL,
    label        TEXT NOT NULL,
    type         TEXT NOT NULL CHECK (type IN ('choice','number','text')),
    options_json TEXT NOT NULL DEFAULT '[]',   -- choice: ["A","B",...]
    bins_json    TEXT NOT NULL DEFAULT '[]',   -- number: ascending edges, e.g. [18,25,35,50,65]
    required     INTEGER NOT NULL DEFAULT 0,
    sort         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (survey_id, key)
);

CREATE TABLE IF NOT EXISTS participants (
    id          TEXT PRIMARY KEY,               -- random UUID
    survey_id   INTEGER NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    demo_done   INTEGER NOT NULL DEFAULT 0,
    finished_at TEXT,
    flagged     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_participants_survey ON participants(survey_id);

CREATE TABLE IF NOT EXISTS participant_answers (
    participant_id TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    question_id    INTEGER NOT NULL REFERENCES demo_questions(id) ON DELETE CASCADE,
    value          TEXT,
    bucket         TEXT,
    PRIMARY KEY (participant_id, question_id)
);

CREATE TABLE IF NOT EXISTS comparisons (
    id             INTEGER PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    survey_id      INTEGER NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    item_left      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    item_right     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    outcome        TEXT CHECK (outcome IN ('left','right','tie','skip')),
    served_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    answered_at    TEXT,
    response_ms    INTEGER,
    UNIQUE (participant_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_comparisons_survey ON comparisons(survey_id, outcome);
CREATE INDEX IF NOT EXISTS ix_comparisons_participant ON comparisons(participant_id);
