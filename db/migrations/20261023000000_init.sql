-- migrate:up
-- okiedoke schema v1.
-- Bookmarks come from X (official API) or the one-time ft import; everything the
-- user does (tags, hides, saved filters) lives in its own tables so a re-sync
-- can never touch it.

CREATE TABLE authors (
    id              bigserial PRIMARY KEY,
    x_user_id       text UNIQUE,              -- NULL for authors known only from the ft import
    handle          text NOT NULL,
    name            text NOT NULL DEFAULT '',
    avatar_url      text,
    avatar_file     text,                     -- filename under MEDIA_DIR once downloaded
    avatar_attempts integer NOT NULL DEFAULT 0
);
CREATE INDEX authors_handle_idx ON authors (lower(handle));

-- Every tweet we know about: bookmarked ones plus the quoted tweets shown inside them.
CREATE TABLE tweets (
    id                   text PRIMARY KEY,    -- X ids exceed 2^53, so text
    author_id            bigint REFERENCES authors (id),
    text                 text NOT NULL DEFAULT '',
    url                  text NOT NULL,
    posted_at            timestamptz,
    like_count           integer NOT NULL DEFAULT 0,
    repost_count         integer NOT NULL DEFAULT 0,
    bookmark_count       integer NOT NULL DEFAULT 0,
    reply_count          integer NOT NULL DEFAULT 0,
    in_reply_to_tweet_id text,
    reply_parent_handle  text,                -- "Replying to @handle"
    quoted_tweet_id      text,                -- no FK: the quoted tweet may be unavailable
    raw                  jsonb,               -- last API payload, for forward compatibility
    updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tweets_posted_at_idx ON tweets (posted_at DESC);

CREATE TABLE media (
    id         bigserial PRIMARY KEY,
    tweet_id   text NOT NULL REFERENCES tweets (id) ON DELETE CASCADE,
    key        text NOT NULL,                 -- X media_key, or 'ft:<filename>' for imported files
    position   integer NOT NULL DEFAULT 0,
    kind       text NOT NULL CHECK (kind IN ('image', 'video')),
    source_url text,
    file       text,                          -- filename under MEDIA_DIR once downloaded
    status     text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'downloaded', 'failed')),
    attempts   integer NOT NULL DEFAULT 0,
    last_error text,
    width      integer,
    height     integer,
    UNIQUE (tweet_id, key)
);
CREATE INDEX media_pending_idx ON media (status) WHERE status = 'pending';

CREATE TABLE bookmarks (
    tweet_id      text PRIMARY KEY REFERENCES tweets (id) ON DELETE CASCADE,
    source        text NOT NULL CHECK (source IN ('x-api', 'ft-import')),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz,                -- last sync that saw it on X
    last_seen_run bigint,                     -- sync_runs.id of that sync (clock-free "was it in this sweep?")
    missing_since timestamptz,                -- a complete sweep no longer saw it => "Deleted on X"
    hidden_at     timestamptz                 -- removed from view by the user (restorable)
);

-- ---- user data ----
CREATE TABLE tags (
    id           bigserial PRIMARY KEY,
    name         text NOT NULL,               -- label as first typed
    name_lower   text NOT NULL UNIQUE,
    last_used_at timestamptz,                 -- drives most-recently-used ordering
    deleted_at   timestamptz,                 -- deleted from every post (restorable; links are kept)
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bookmark_tags (
    tweet_id text   NOT NULL REFERENCES bookmarks (tweet_id) ON DELETE CASCADE,
    tag_id   bigint NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    seq      bigserial,                       -- insertion order; "newest tag" = highest seq
    PRIMARY KEY (tweet_id, tag_id)
);
CREATE INDEX bookmark_tags_tag_idx ON bookmark_tags (tag_id);

CREATE TABLE saved_filters (
    id         bigserial PRIMARY KEY,
    name       text NOT NULL,
    name_lower text NOT NULL UNIQUE,
    expr       text NOT NULL,
    seq        bigserial
);

-- ---- sync ----
CREATE TABLE sync_runs (
    id               bigserial PRIMARY KEY,
    kind             text NOT NULL CHECK (kind IN ('incremental', 'full')),
    trigger          text NOT NULL,           -- manual | auto | schedule
    status           text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'partial', 'error')),
    phase            text,                    -- fetching | media | done
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    pages            integer NOT NULL DEFAULT 0,
    fetched          integer NOT NULL DEFAULT 0,
    inserted         integer NOT NULL DEFAULT 0,
    updated          integer NOT NULL DEFAULT 0,
    marked_missing   integer NOT NULL DEFAULT 0,
    media_downloaded integer NOT NULL DEFAULT 0,
    media_failed     integer NOT NULL DEFAULT 0,
    complete         boolean NOT NULL DEFAULT false,  -- a full sweep that reached the end of the list
    error            text
);
CREATE INDEX sync_runs_started_idx ON sync_runs (started_at DESC);

-- The single connected X account. Tokens are Fernet-encrypted with SECRET_KEY.
CREATE TABLE x_auth (
    id                int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    x_user_id         text NOT NULL,
    handle            text,
    access_token_enc  text NOT NULL,
    refresh_token_enc text NOT NULL,
    expires_at        timestamptz NOT NULL,
    scope             text,
    status            text NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'needs_reauth')),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE oauth_states (
    state         text PRIMARY KEY,
    code_verifier text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Verbatim copies of whatever was imported (e.g. the old okiedoke-tags.json) so
-- nothing the user ever created can be lost in translation.
CREATE TABLE legacy_imports (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL,
    source_path text,
    payload     jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);

-- The FastAPI process enters this role through its direct connection URI. On Kamaji
-- these roles are provisioned by `bin/kamaji add-app`; the bundled dev database uses
-- its own owner and needs no role switch.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'okiedoke_anon') THEN
    EXECUTE 'GRANT USAGE ON SCHEMA public TO okiedoke_anon';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO okiedoke_anon';
    EXECUTE 'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO okiedoke_anon';
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'harsh') THEN
      EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE harsh IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO okiedoke_anon';
      EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE harsh IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO okiedoke_anon';
    END IF;
  END IF;
END $$;

-- migrate:down
DROP TABLE IF EXISTS legacy_imports, oauth_states, x_auth, sync_runs, saved_filters,
  bookmark_tags, tags, bookmarks, media, tweets, authors CASCADE;
