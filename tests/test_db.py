def test_migrate_is_idempotent(_db):
    assert _db.migrate() == []
    with _db.connection() as conn:
        versions = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == ["001_init"]


def test_expected_tables_exist(_db):
    with _db.connection() as conn:
        names = {r["tablename"] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"tweets", "bookmarks", "media", "authors", "tags", "bookmark_tags",
            "saved_filters", "sync_runs", "x_auth", "oauth_states", "legacy_imports"} <= names
