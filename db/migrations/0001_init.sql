-- Initial schema. ARCHITECTURE.md §4 explains each table.

CREATE TABLE users (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role          text NOT NULL DEFAULT 'participant' CHECK (role IN ('participant', 'admin')),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_clients (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text NOT NULL,
    key_hash     text NOT NULL UNIQUE,
    max_inflight int NOT NULL DEFAULT 5 CHECK (max_inflight > 0),
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE problems (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug            text NOT NULL UNIQUE,
    title           text NOT NULL,
    statement_md    text NOT NULL,
    time_limit_ms   int NOT NULL CHECK (time_limit_ms BETWEEN 100 AND 10000),
    memory_limit_mb int NOT NULL CHECK (memory_limit_mb BETWEEN 16 AND 512),
    output_limit_kb int NOT NULL DEFAULT 16384 CHECK (output_limit_kb > 0),
    checker         text NOT NULL DEFAULT 'token' CHECK (checker IN ('exact', 'token')),
    is_public       boolean NOT NULL DEFAULT false
);

CREATE TABLE testcases (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    problem_id    bigint NOT NULL REFERENCES problems ON DELETE CASCADE,
    ordinal       int NOT NULL,
    input_text    text NOT NULL,
    expected_text text NOT NULL,
    is_sample     boolean NOT NULL DEFAULT false,
    UNIQUE (problem_id, ordinal)
);

CREATE TABLE contests (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title       text NOT NULL,
    invite_code text NOT NULL UNIQUE,
    starts_at   timestamptz NOT NULL,
    ends_at     timestamptz NOT NULL,
    CHECK (ends_at > starts_at)
);

CREATE TABLE contest_problems (
    contest_id bigint NOT NULL REFERENCES contests ON DELETE CASCADE,
    problem_id bigint NOT NULL REFERENCES problems,
    label      text NOT NULL,
    PRIMARY KEY (contest_id, problem_id),
    UNIQUE (contest_id, label)
);

-- One row per owner per time window. A request does one upsert and reads back the count.
CREATE TABLE rate_limits (
    owner_type   text NOT NULL CHECK (owner_type IN ('user', 'client')),
    owner_id     bigint NOT NULL,
    window_start timestamptz NOT NULL,
    count        int NOT NULL,
    PRIMARY KEY (owner_type, owner_id, window_start)
);

-- The queue. A job's id is also its MCP task id, so it must be random and unguessable.
CREATE TABLE jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             text NOT NULL CHECK (kind IN ('submission', 'run', 'verify')),
    owner_type       text NOT NULL CHECK (owner_type IN ('user', 'client')),
    owner_id         bigint NOT NULL,
    idempotency_key  text,
    request_hash     bytea NOT NULL,
    language         text NOT NULL CHECK (language IN ('cpp', 'python')),
    source           text NOT NULL,
    stdin            text,
    tests            jsonb,
    problem_id       bigint REFERENCES problems,
    contest_id       bigint REFERENCES contests,
    priority         smallint NOT NULL CHECK (priority IN (0, 1, 2)),  -- 0 contest, 1 practice, 2 agent
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'completed', 'cancelled', 'dead')),
    attempts         int NOT NULL DEFAULT 0,
    max_attempts     int NOT NULL DEFAULT 3,
    not_before       timestamptz NOT NULL DEFAULT now(),
    worker_id        text,
    lease_until      timestamptz,
    fencing_token    bigint NOT NULL DEFAULT 0,
    container_id     text,
    cancel_requested boolean NOT NULL DEFAULT false,
    last_error       text,
    submitted_at     timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    expires_at       timestamptz,
    rejudge_of       uuid REFERENCES jobs ON DELETE SET NULL,
    superseded       boolean NOT NULL DEFAULT false,
    -- A running job always has an owner and a lease
    CHECK (status <> 'running' OR (worker_id IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK (kind <> 'submission' OR problem_id IS NOT NULL),
    CHECK (kind <> 'verify' OR tests IS NOT NULL)
);

-- One job per idempotency key per owner. Requests without a key are never deduplicated.
CREATE UNIQUE INDEX jobs_idempotency ON jobs (owner_type, owner_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
-- The claim query reads queued jobs in exactly this order
CREATE INDEX jobs_queue ON jobs (priority, created_at) WHERE status = 'queued';
-- The reaper looks for running jobs whose lease has run out
CREATE INDEX jobs_leases ON jobs (lease_until) WHERE status = 'running';
-- Counting an owner's queued and running jobs, for the inflight cap
CREATE INDEX jobs_owner_active ON jobs (owner_type, owner_id) WHERE status IN ('queued', 'running');
CREATE INDEX jobs_expiry ON jobs (expires_at) WHERE expires_at IS NOT NULL;

-- job_id is the primary key, so a job can have at most one result. The database enforces it.
CREATE TABLE results (
    job_id         uuid PRIMARY KEY REFERENCES jobs ON DELETE CASCADE,
    verdict        text NOT NULL CHECK (verdict IN ('AC', 'WA', 'TLE', 'MLE', 'OLE', 'RE', 'CE', 'OK', 'IE')),
    per_test       jsonb NOT NULL DEFAULT '[]',
    peak_memory_kb bigint,
    failed_test    int,
    stdout_trunc   text CHECK (octet_length(stdout_trunc) <= 65536),
    stderr_trunc   text CHECK (octet_length(stderr_trunc) <= 65536),
    compile_log    text CHECK (octet_length(compile_log) <= 65536),
    created_at     timestamptz NOT NULL DEFAULT now()
);
