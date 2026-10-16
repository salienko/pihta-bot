-- All objects live in a dedicated schema; previous booking tables are untouched.
CREATE TABLE IF NOT EXISTS customers (
 telegram_id bigint PRIMARY KEY, username text NOT NULL DEFAULT '',
 name text NOT NULL, phone text NOT NULL, updated_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
 telegram_id bigint PRIMARY KEY, token text NOT NULL UNIQUE,
 state text NOT NULL, data jsonb NOT NULL, updated_at timestamptz NOT NULL,
 revision integer NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
 id text PRIMARY KEY, draft_token text NOT NULL UNIQUE,
 telegram_id bigint NOT NULL REFERENCES customers(telegram_id),
 data jsonb NOT NULL, estimate jsonb NOT NULL, rules_hash text NOT NULL,
 consent_at timestamptz NOT NULL, created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_customer ON orders(telegram_id, created_at);
CREATE TABLE IF NOT EXISTS deliveries (
 id bigserial PRIMARY KEY,
 order_id text NOT NULL REFERENCES orders(id), admin_id bigint NOT NULL,
 text text NOT NULL, attempts integer NOT NULL DEFAULT 0,
 due_at timestamptz NOT NULL DEFAULT now(), locked_until timestamptz,
 delivered_at timestamptz, last_error text,
 UNIQUE(order_id, admin_id)
);
CREATE INDEX IF NOT EXISTS deliveries_pending ON deliveries(due_at) WHERE delivered_at IS NULL;
