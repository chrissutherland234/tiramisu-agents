-- Local development only. Production creates equivalent least-privilege roles
-- through infrastructure automation and supplies secrets from a secret manager.
CREATE ROLE tiramisu_app LOGIN PASSWORD 'tiramisu_app' NOSUPERUSER NOCREATEDB NOCREATEROLE;
DO $grant_database$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO tiramisu_app',
        current_database()
    );
END
$grant_database$;
GRANT USAGE ON SCHEMA public TO tiramisu_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO tiramisu_app;
