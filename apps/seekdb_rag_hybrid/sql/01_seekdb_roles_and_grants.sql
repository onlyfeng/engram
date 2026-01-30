-- SeekDB roles and grants
-- seekdb_migrator / seekdb_app / seek_app (compat)

GRANT USAGE ON SCHEMA logbook TO seekdb_app;
GRANT USAGE ON SCHEMA logbook TO seek_app;
GRANT USAGE ON SCHEMA logbook TO seekdb_migrator;

GRANT SELECT, INSERT, UPDATE ON logbook.kv TO seekdb_app;
GRANT SELECT, INSERT, UPDATE ON logbook.kv TO seek_app;
GRANT SELECT, INSERT, UPDATE ON logbook.kv TO seekdb_migrator;

GRANT SELECT ON logbook.attachments TO seekdb_app;
GRANT SELECT ON logbook.attachments TO seek_app;
GRANT SELECT ON logbook.attachments TO seekdb_migrator;

GRANT INSERT ON logbook.events TO seekdb_app;
GRANT INSERT ON logbook.events TO seek_app;
GRANT INSERT ON logbook.events TO seekdb_migrator;

GRANT SELECT ON scm.patch_blobs TO seekdb_app;
GRANT SELECT ON scm.patch_blobs TO seek_app;
GRANT SELECT ON scm.patch_blobs TO seekdb_migrator;
