"""Workspace base schema. SQLite pragmas are applied only by its backend."""

BASE_SCHEMA = """
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL,
          password_hash TEXT, role TEXT NOT NULL, issuer TEXT NOT NULL DEFAULT 'local',
          subject TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
          UNIQUE(issuer,subject));
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
          csrf TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS connections (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, endpoint TEXT NOT NULL,
          provider TEXT NOT NULL, secret TEXT, status TEXT NOT NULL,
          last_checked REAL, last_error TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS deployments (
          id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(id),
          model TEXT NOT NULL, capabilities TEXT NOT NULL, verified TEXT NOT NULL DEFAULT '[]',
          input_price REAL, output_price REAL, enabled INTEGER NOT NULL DEFAULT 1,
          UNIQUE(connection_id,model));
        CREATE TABLE IF NOT EXISTS ports (
          id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT NOT NULL DEFAULT '',
          deployments TEXT NOT NULL, requirements TEXT NOT NULL DEFAULT '[]',
          strategy TEXT NOT NULL DEFAULT 'priority', version INTEGER NOT NULL DEFAULT 1,
          enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, monthly_budget REAL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS api_keys (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
          prefix TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id),
          allowed_ports TEXT NOT NULL, rpm INTEGER NOT NULL DEFAULT 60,
          active INTEGER NOT NULL DEFAULT 1, expires REAL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          id TEXT PRIMARY KEY, started REAL NOT NULL, finished REAL, principal TEXT NOT NULL,
          project_id TEXT, key_id TEXT, port_id TEXT, port_name TEXT, port_version INTEGER,
          deployment_id TEXT, model TEXT, connection_name TEXT, status TEXT NOT NULL,
          stream INTEGER NOT NULL, latency_ms REAL, ttft_ms REAL,
          input_tokens INTEGER, output_tokens INTEGER, usage_source TEXT NOT NULL DEFAULT 'unknown',
          cost_micros INTEGER, reserved_micros INTEGER NOT NULL DEFAULT 0,
          decisions TEXT NOT NULL DEFAULT '[]', error TEXT);
        CREATE INDEX IF NOT EXISTS events_time ON events(started);
        CREATE INDEX IF NOT EXISTS events_project ON events(project_id,started);
        CREATE INDEX IF NOT EXISTS events_key ON events(key_id,started);
        CREATE TABLE IF NOT EXISTS bindings (
          id TEXT PRIMARY KEY, principal TEXT NOT NULL, port_id TEXT NOT NULL REFERENCES ports(id),
          version INTEGER NOT NULL, fingerprint TEXT NOT NULL, requirements TEXT NOT NULL,
          expires REAL NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS identity_providers (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, issuer TEXT NOT NULL,
          client_id TEXT NOT NULL, secret TEXT NOT NULL, default_role TEXT NOT NULL DEFAULT 'viewer',
          active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS audit (
          id TEXT PRIMARY KEY, created REAL NOT NULL, actor TEXT NOT NULL,
          action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
