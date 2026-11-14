-- Attempt counters for login and registration (DECISIONS.md, 102). One row per key per time window.
-- key_hash is sha256 of 'address:<ip>' or 'email:<email>': equality is all the limit needs,
-- so the table never holds a raw address or email.
CREATE TABLE login_limits (
    key_hash     bytea NOT NULL,
    window_start timestamptz NOT NULL,
    count        int NOT NULL,
    PRIMARY KEY (key_hash, window_start)
);
