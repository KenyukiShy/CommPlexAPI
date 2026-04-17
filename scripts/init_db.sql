-- scripts/init_db.sql
-- Runs automatically when the postgres container first starts.
-- Mirrors the production GCP Cloud SQL schema.

CREATE TABLE IF NOT EXISTS contacts (
    id           SERIAL PRIMARY KEY,
    campaign_id  VARCHAR(50)  NOT NULL,
    contact_name VARCHAR(100) NOT NULL,
    email        VARCHAR(200),
    phone        VARCHAR(20),
    tier         VARCHAR(50)  DEFAULT 'DEFAULT',
    method       VARCHAR(20)  DEFAULT 'email',
    status       VARCHAR(30)  DEFAULT 'PENDING',
    notes        TEXT,
    created_at   TIMESTAMPTZ  DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE(campaign_id, contact_name)
);

CREATE TABLE IF NOT EXISTS campaign_events (
    event_id     VARCHAR(36)  PRIMARY KEY,
    campaign_id  VARCHAR(50)  NOT NULL,
    contact_name VARCHAR(100),
    module       VARCHAR(30),
    status       VARCHAR(30),
    tier         VARCHAR(50),
    timestamp    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    metadata     TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_campaign ON campaign_events(campaign_id);
CREATE INDEX IF NOT EXISTS idx_events_status   ON campaign_events(status);
CREATE INDEX IF NOT EXISTS idx_contacts_campaign ON contacts(campaign_id);
