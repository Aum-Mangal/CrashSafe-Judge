-- The leaderboard is public, so it shows a name the user chose, never their email (DECISIONS.md, 110).
-- Accounts made before this get a placeholder, then the column becomes required.
ALTER TABLE users ADD COLUMN display_name text;
UPDATE users SET display_name = 'Contestant ' || id;
ALTER TABLE users ALTER COLUMN display_name SET NOT NULL,
    ADD CONSTRAINT users_display_name_length CHECK (char_length(display_name) BETWEEN 1 AND 40);
