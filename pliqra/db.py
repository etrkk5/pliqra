import json
import sqlite3
import threading
import time
import uuid
import os
import fcntl
import shutil
from contextlib import contextmanager
from pathlib import Path


def uid(prefix=""):
    return prefix + uuid.uuid4().hex[:20]


def now():
    return time.time()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        self.lease = open(directory / "instance.lock", "a+")
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lease.close()
            raise RuntimeError("This data volume is already in use. Run one Pliqra process per volume.")
        database_name = 'pliqra.sqlite3'
        legacy = directory / 'modelport.sqlite3'
        if legacy.exists() and not (directory / database_name).exists():
            database_name = legacy.name
        if (directory/'pending-restore').exists():
            # An interrupted restore is rolled back before opening the database or vault.
            for name,source in [(database_name,'pre-restore.sqlite3'),('encryption.key','pre-restore.key')]:
                temporary=directory/(name+'.recovery')
                shutil.copyfile(directory/source,temporary)
                os.chmod(temporary,0o600)
                os.replace(temporary,directory/name)
            for suffix in ('-wal','-shm'):
                (directory/(database_name+suffix)).unlink(missing_ok=True)
            (directory/'pending-restore').unlink()
        if legacy.exists():
            current = directory / 'pliqra.sqlite3'
            if current.exists():
                self.lease.close()
                raise RuntimeError('Both legacy and Pliqra databases exist. Select one workspace volume before starting.')
            # Checkpoint the old WAL before atomically renaming the database.
            # The shared instance lease prevents the old and new servers running together.
            previous = sqlite3.connect(legacy)
            try:
                if previous.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                    raise RuntimeError('Legacy database is busy. Stop the previous server before upgrading.')
            finally:
                previous.close()
            os.replace(legacy, current)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(directory / "pliqra.sqlite3", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
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
        """)
        self.migrate()
        # Work interrupted by a restart must remain visibly incomplete, never become a success.
        self.conn.execute("UPDATE events SET status='interrupted',error='Gateway restarted before completion; usage requires reconciliation',finished=? WHERE status='pending'", (now(),))
        if not self.one("SELECT id FROM projects LIMIT 1"):
            self.run("INSERT INTO projects (id,name,monthly_budget,created) VALUES (?,?,?,?)", ("default", "Default workspace", None, now()))

    def migrate(self):
        additions = {
            "connections": {"enabled": "INTEGER NOT NULL DEFAULT 1", "health_interval": "INTEGER NOT NULL DEFAULT 300", "manual_model": "TEXT NOT NULL DEFAULT ''", "auth_header": "TEXT NOT NULL DEFAULT 'Authorization'", "auth_prefix": "TEXT NOT NULL DEFAULT 'Bearer'"},
            "deployments": {"metadata_hash": "TEXT NOT NULL DEFAULT ''", "available": "INTEGER NOT NULL DEFAULT 1", "failure_count": "INTEGER NOT NULL DEFAULT 0", "open_until": "REAL NOT NULL DEFAULT 0", "verified_at": "REAL"},
            "ports": {"project_id": "TEXT", "verified_only": "INTEGER NOT NULL DEFAULT 0", "cache_ttl": "INTEGER NOT NULL DEFAULT 0", "max_input_chars": "INTEGER NOT NULL DEFAULT 200000", "max_output_tokens": "INTEGER NOT NULL DEFAULT 32768", "log_content": "INTEGER NOT NULL DEFAULT 0"},
            "projects": {"max_concurrency": "INTEGER NOT NULL DEFAULT 20"},
            "api_keys": {"created_by": "TEXT", "team_id": "TEXT", "gateway_id": "TEXT"},
            "bindings": {"gateway_id": "TEXT", "gateway_fingerprint": "TEXT"},
            "events": {"request_kind": "TEXT NOT NULL DEFAULT 'inference'", "reconciled_by": "TEXT", "reconciliation_note": "TEXT", "cache_hit": "INTEGER NOT NULL DEFAULT 0", "price_snapshot": "TEXT", "team_id": "TEXT", "actor_user_id": "TEXT", "known_cost_micros": "INTEGER NOT NULL DEFAULT 0"},
            "identity_providers": {"protocol": "TEXT NOT NULL DEFAULT 'oidc'", "config": "TEXT NOT NULL DEFAULT '{}'"},
            "users": {"external_id": "TEXT", "groups": "TEXT NOT NULL DEFAULT '[]'", "username": "TEXT NOT NULL DEFAULT ''"},
        }
        additions["events"].update(gateway_id="TEXT", gateway_name="TEXT", gateway_fingerprint="TEXT", delivery="TEXT")
        with self.transaction():
            for table, columns in additions.items():
                existing = {row["name"] for row in self.all(f"PRAGMA table_info({table})")}
                for name, definition in columns.items():
                    if name not in existing:
                        self.run(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            for statement in [
                "CREATE TABLE IF NOT EXISTS gateway_profiles (id TEXT PRIMARY KEY,name TEXT NOT NULL,slug TEXT NOT NULL UNIQUE,config TEXT NOT NULL,version INTEGER NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS gateway_versions (gateway_id TEXT NOT NULL,version INTEGER NOT NULL,document TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(gateway_id,version))",
                "CREATE TABLE IF NOT EXISTS guardrails (id TEXT PRIMARY KEY,name TEXT NOT NULL,provider TEXT NOT NULL,config TEXT NOT NULL,secret TEXT,version INTEGER NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL,updated REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS gateway_requests (id TEXT PRIMARY KEY,gateway_id TEXT NOT NULL,fingerprint TEXT NOT NULL,principal TEXT NOT NULL,user_id TEXT,key_id TEXT,project_id TEXT,team_id TEXT,port_id TEXT NOT NULL,started REAL NOT NULL,status TEXT NOT NULL,tokens INTEGER NOT NULL,reservation INTEGER NOT NULL)",
                "CREATE INDEX IF NOT EXISTS gateway_requests_window ON gateway_requests(gateway_id,started)",
                "CREATE TABLE IF NOT EXISTS guardrail_decisions (id TEXT PRIMARY KEY,event_id TEXT,gateway_id TEXT,guardrail_id TEXT NOT NULL,name TEXT NOT NULL,stage TEXT NOT NULL,outcome TEXT NOT NULL,mode TEXT NOT NULL,on_error TEXT NOT NULL,latency_ms REAL NOT NULL,created REAL NOT NULL)",
                "CREATE INDEX IF NOT EXISTS guardrail_decisions_event ON guardrail_decisions(event_id)",
                "CREATE TABLE IF NOT EXISTS memberships (project_id TEXT NOT NULL REFERENCES projects(id),user_id TEXT NOT NULL REFERENCES users(id),role TEXT NOT NULL,source TEXT NOT NULL DEFAULT 'manual',PRIMARY KEY(project_id,user_id,source))",
                "CREATE TABLE IF NOT EXISTS teams (id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS team_members (team_id TEXT NOT NULL REFERENCES teams(id),user_id TEXT NOT NULL REFERENCES users(id),PRIMARY KEY(team_id,user_id))",
                "CREATE TABLE IF NOT EXISTS team_projects (team_id TEXT NOT NULL REFERENCES teams(id),project_id TEXT NOT NULL REFERENCES projects(id),role TEXT NOT NULL DEFAULT 'member',PRIMARY KEY(team_id,project_id))",
                "CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY,event_id TEXT NOT NULL REFERENCES events(id),deployment_id TEXT NOT NULL,model TEXT NOT NULL,started REAL NOT NULL,finished REAL,outcome TEXT NOT NULL,http_status INTEGER,input_tokens INTEGER,output_tokens INTEGER,cost_micros INTEGER)",
                "CREATE INDEX IF NOT EXISTS attempts_event ON attempts(event_id)",
                "CREATE INDEX IF NOT EXISTS events_project_time ON events(project_id,started)",
                "CREATE INDEX IF NOT EXISTS events_team_time ON events(team_id,started)",
                "CREATE INDEX IF NOT EXISTS events_actor_time ON events(actor_user_id,started)",
                "CREATE TABLE IF NOT EXISTS event_content (id TEXT PRIMARY KEY REFERENCES events(id),payload TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS port_versions (port_id TEXT NOT NULL,version INTEGER NOT NULL,config TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(port_id,version))",
                "CREATE TABLE IF NOT EXISTS probes (id TEXT PRIMARY KEY,deployment_id TEXT NOT NULL,capability TEXT NOT NULL,passed INTEGER NOT NULL,request_id TEXT,detail TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS evaluation_sets (id TEXT PRIMARY KEY,name TEXT NOT NULL,cases TEXT NOT NULL,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS evaluations (id TEXT PRIMARY KEY,name TEXT NOT NULL,dataset_id TEXT NOT NULL,services TEXT NOT NULL,status TEXT NOT NULL,results TEXT NOT NULL DEFAULT '[]',created REAL NOT NULL,finished REAL,actor TEXT NOT NULL,max_tokens INTEGER NOT NULL,store_outputs INTEGER NOT NULL DEFAULT 0,error TEXT)",
                "CREATE TABLE IF NOT EXISTS cache_entries (id TEXT PRIMARY KEY,port_id TEXT NOT NULL,payload TEXT NOT NULL,expires REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY,kind TEXT NOT NULL,target TEXT NOT NULL,message TEXT NOT NULL,created REAL NOT NULL,resolved INTEGER NOT NULL DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS scim_tokens (id TEXT PRIMARY KEY,provider_id TEXT NOT NULL REFERENCES identity_providers(id),token_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS identity_groups (id TEXT PRIMARY KEY,provider_id TEXT NOT NULL,external_id TEXT,name TEXT NOT NULL,members TEXT NOT NULL DEFAULT '[]',UNIQUE(provider_id,external_id))",
                "CREATE TABLE IF NOT EXISTS group_rules (id TEXT PRIMARY KEY,provider_id TEXT NOT NULL,group_name TEXT NOT NULL,project_id TEXT NOT NULL REFERENCES projects(id),role TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS saml_flows (id TEXT PRIMARY KEY,provider_id TEXT NOT NULL,request_id TEXT NOT NULL,expires REAL NOT NULL)",
                "CREATE TABLE IF NOT EXISTS saml_assertions (id TEXT PRIMARY KEY,expires REAL NOT NULL)",
            ]:
                self.run(statement)
            self.run("PRAGMA user_version=3")
            self.run("UPDATE gateway_requests SET status='interrupted' WHERE status='pending'")
        self.run("UPDATE evaluations SET status='interrupted',error='Gateway restarted during evaluation',finished=? WHERE status IN ('queued','running')", (now(),))

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        self.run("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encode(value)))

    def all(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def run(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def insert(self, table, fields):
        names = list(fields)
        self.run(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", tuple(fields.values()))

    def update(self, table, record_id, fields):
        self.run(f"UPDATE {table} SET {','.join(name+'=?' for name in fields)} WHERE id=?", (*fields.values(), record_id))

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def audit(self, actor, action, target, detail=None):
        self.run("INSERT INTO audit VALUES (?,?,?,?,?,?)", (uid("aud_"), now(), actor, action, target, encode(detail or {})))

    def close(self):
        self.conn.close()
        self.lease.close()
