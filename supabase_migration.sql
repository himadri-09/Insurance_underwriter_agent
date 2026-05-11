-- TriagePilot: Supabase schema
-- Run this in the Supabase SQL editor

-- ─── Submissions table ──────────────────────────────

CREATE TABLE IF NOT EXISTS submissions (
    id              TEXT PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),

    -- summary fields
    status          TEXT DEFAULT 'uploaded',
    lob             TEXT DEFAULT 'other',
    insured_name    TEXT DEFAULT '',
    appetite_score  INTEGER DEFAULT 0,
    appetite_status TEXT DEFAULT 'review',
    winnability     FLOAT DEFAULT 0.0,
    priority        FLOAT DEFAULT 0.0,
    queue           TEXT DEFAULT 'general',
    referral_required BOOLEAN DEFAULT FALSE,

    -- generated outputs
    brief_markdown  TEXT DEFAULT '',
    processing_time FLOAT DEFAULT 0.0,

    -- full result
    result_json     JSONB DEFAULT '{}',

    -- errors
    errors          JSONB DEFAULT '[]'
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions(user_id);
CREATE INDEX IF NOT EXISTS idx_submissions_user_created ON submissions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_submissions_lob ON submissions(lob);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER submissions_updated_at
    BEFORE UPDATE ON submissions
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();


-- ─── Row Level Security ─────────────────────────────
-- Users can only see/modify their own submissions.

ALTER TABLE submissions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own submissions"
    ON submissions FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own submissions"
    ON submissions FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own submissions"
    ON submissions FOR UPDATE
    USING (auth.uid() = user_id);

CREATE POLICY "Users can delete own submissions"
    ON submissions FOR DELETE
    USING (auth.uid() = user_id);

-- Service role bypasses RLS (for backend writes with service key)
-- This is automatic in Supabase when using the service_role key.


-- ─── Audit log table ────────────────────────────────

CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    submission_id   TEXT REFERENCES submissions(id) ON DELETE CASCADE,
    user_id         UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    stage           TEXT NOT NULL,
    agent           TEXT DEFAULT '',
    model           TEXT DEFAULT '',
    prompt_hash     TEXT DEFAULT '',
    input_summary   TEXT DEFAULT '',
    output_summary  TEXT DEFAULT '',
    tool_calls      JSONB DEFAULT '[]',
    duration_ms     INTEGER DEFAULT 0,
    tokens_in       INTEGER DEFAULT 0,
    tokens_out      INTEGER DEFAULT 0,
    error           TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_submission ON audit_log(submission_id);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own audit logs"
    ON audit_log FOR SELECT
    USING (auth.uid() = user_id);


-- ─── Storage policies ───────────────────────────────
-- Create bucket "submissions" via Supabase dashboard first.
-- These policies scope file access to the owning user.

-- Users can upload to their own folder: submissions/{user_id}/*
CREATE POLICY "Users can upload own files"
    ON storage.objects FOR INSERT
    WITH CHECK (
        bucket_id = 'submissions'
        AND (storage.foldername(name))[1] = auth.uid()::text
    );

-- Users can read their own files
CREATE POLICY "Users can read own files"
    ON storage.objects FOR SELECT
    USING (
        bucket_id = 'submissions'
        AND (storage.foldername(name))[1] = auth.uid()::text
    );

-- Users can delete their own files
CREATE POLICY "Users can delete own files"
    ON storage.objects FOR DELETE
    USING (
        bucket_id = 'submissions'
        AND (storage.foldername(name))[1] = auth.uid()::text
    );
