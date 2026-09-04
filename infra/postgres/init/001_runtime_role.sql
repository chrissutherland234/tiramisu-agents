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
REVOKE CREATE ON SCHEMA public FROM tiramisu_app;

-- Application migrations grant each table privilege explicitly. Do not use
-- default privileges here: a newly created table must remain inaccessible
-- until its migration deliberately adds it to the runtime-role contract.
