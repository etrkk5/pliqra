"""Portable independent-instance management tables; no raw inference content."""
SCHEMA = [
    "CREATE TABLE IF NOT EXISTS fleet_trusts (id TEXT PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,prefix TEXT NOT NULL,scopes TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created DOUBLE PRECISION NOT NULL,last_used DOUBLE PRECISION)",
    "CREATE TABLE IF NOT EXISTS fleet_members (id TEXT PRIMARY KEY,name TEXT NOT NULL,endpoint TEXT NOT NULL UNIQUE,secret TEXT NOT NULL,workspace_id TEXT NOT NULL UNIQUE,enabled INTEGER NOT NULL DEFAULT 1,created DOUBLE PRECISION NOT NULL,last_sync DOUBLE PRECISION,last_error TEXT,snapshot TEXT NOT NULL DEFAULT '{}',sync_claim TEXT,sync_until DOUBLE PRECISION NOT NULL DEFAULT 0,next_sync DOUBLE PRECISION NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS fleet_managed (id TEXT PRIMARY KEY,trust_id TEXT NOT NULL,gateway_id TEXT NOT NULL,desired_fingerprint TEXT NOT NULL,applied_fingerprint TEXT NOT NULL,updated DOUBLE PRECISION NOT NULL)",
    "CREATE TABLE IF NOT EXISTS fleet_receipts (id TEXT PRIMARY KEY,trust_id TEXT NOT NULL,plan_hash TEXT NOT NULL,receipt TEXT NOT NULL,created DOUBLE PRECISION NOT NULL)",
    "CREATE INDEX IF NOT EXISTS fleet_receipts_created ON fleet_receipts(created)",
    "CREATE TABLE IF NOT EXISTS fleet_proposals (id TEXT PRIMARY KEY,name TEXT NOT NULL,document TEXT NOT NULL,fingerprint TEXT NOT NULL,author TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',approved_by TEXT,created DOUBLE PRECISION NOT NULL,approved DOUBLE PRECISION)",
    "CREATE TABLE IF NOT EXISTS fleet_targets (id TEXT PRIMARY KEY,proposal_id TEXT NOT NULL,member_id TEXT NOT NULL,plan TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',claim TEXT,claim_until DOUBLE PRECISION NOT NULL DEFAULT 0,receipt TEXT,last_error TEXT,updated DOUBLE PRECISION NOT NULL,UNIQUE(proposal_id,member_id))",
]
