CREATE TABLE IF NOT EXISTS profiles(telegram_id bigint PRIMARY KEY, name text NOT NULL, phone text NOT NULL);
CREATE TABLE IF NOT EXISTS drafts(telegram_id bigint PRIMARY KEY,token text NOT NULL UNIQUE,state text NOT NULL,data jsonb NOT NULL,revision integer NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS bookings(
 id text PRIMARY KEY, draft_token text NOT NULL UNIQUE, telegram_id bigint NOT NULL,
 starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL, blocked_until timestamptz NOT NULL,
 data jsonb NOT NULL, estimate jsonb NOT NULL, rules_hash text NOT NULL, created_at timestamptz NOT NULL,
 status text NOT NULL CHECK(status IN ('pending','confirmed','cancelled','rejected','expired','completed')),
 reason text NOT NULL DEFAULT '', paid bigint NOT NULL DEFAULT 0 CHECK(paid>=0),
 actual_guests integer CHECK(actual_guests BETWEEN 1 AND 8), actual_total bigint CHECK(actual_total>=0),
 no_show boolean NOT NULL DEFAULT false, revision integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS bookings_customer ON bookings(telegram_id,starts_at);
CREATE INDEX IF NOT EXISTS bookings_calendar ON bookings(starts_at,blocked_until);
CREATE TABLE IF NOT EXISTS audit(id bigserial PRIMARY KEY,booking_id text REFERENCES bookings(id),actor bigint NOT NULL,action text NOT NULL,detail jsonb NOT NULL DEFAULT '{}',created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS outbox(id bigserial PRIMARY KEY,booking_id text NOT NULL REFERENCES bookings(id),recipient bigint NOT NULL,event text NOT NULL,text text NOT NULL,due_at timestamptz NOT NULL DEFAULT now(),attempts integer NOT NULL DEFAULT 0,locked_until timestamptz,delivered_at timestamptz,disabled boolean NOT NULL DEFAULT false,last_error text,UNIQUE(booking_id,recipient,event));
CREATE TABLE IF NOT EXISTS web_tokens(hash text PRIMARY KEY,actor bigint NOT NULL,expires_at timestamptz NOT NULL,used boolean NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS web_sessions(hash text PRIMARY KEY,actor bigint NOT NULL,csrf text NOT NULL,expires_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS operations(key text PRIMARY KEY,actor bigint NOT NULL,result text NOT NULL);
CREATE TABLE IF NOT EXISTS service_settings(code text PRIMARY KEY,available boolean NOT NULL);
CREATE TABLE IF NOT EXISTS holiday_rates(day date PRIMARY KEY,label text NOT NULL,base bigint NOT NULL CHECK(base>0 AND base%2=0),extra bigint NOT NULL CHECK(extra>0));
CREATE TABLE IF NOT EXISTS calendar_blocks(id bigserial PRIMARY KEY,starts_at timestamptz NOT NULL,ends_at timestamptz NOT NULL,reason text NOT NULL,actor bigint NOT NULL,active boolean NOT NULL DEFAULT true,CHECK(ends_at>starts_at));
CREATE OR REPLACE FUNCTION protect_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.id,NEW.telegram_id,NEW.draft_token,NEW.starts_at,NEW.ends_at,NEW.blocked_until,NEW.data,NEW.estimate,NEW.rules_hash,NEW.created_at) IS DISTINCT FROM ROW(OLD.id,OLD.telegram_id,OLD.draft_token,OLD.starts_at,OLD.ends_at,OLD.blocked_until,OLD.data,OLD.estimate,OLD.rules_hash,OLD.created_at) THEN RAISE EXCEPTION 'booking snapshot is immutable'; END IF;
 RETURN NEW;
END;$$;
DROP TRIGGER IF EXISTS snapshot_immutable ON bookings;
CREATE TRIGGER snapshot_immutable BEFORE UPDATE ON bookings FOR EACH ROW EXECUTE FUNCTION protect_snapshot();
