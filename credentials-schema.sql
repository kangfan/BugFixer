-- Passwords are written by BugFixer using Windows DPAPI, not plaintext SQL literals.
CREATE TABLE IF NOT EXISTS web_credentials (
    name TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK(source IN ('zentao','customer_service')),
    environment TEXT NOT NULL DEFAULT 'production' CHECK(environment IN ('production','test')),
    login_url TEXT NOT NULL,
    account TEXT NOT NULL,
    password_encrypted BLOB NOT NULL,
    protection TEXT NOT NULL DEFAULT 'Windows DPAPI current user',
    updated_at TEXT NOT NULL
);
