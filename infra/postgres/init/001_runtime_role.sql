-- Local development only. Production creates equivalent least-privilege roles
-- through infrastructure automation and supplies secrets from a secret manager.
CREATE ROLE tiramisu_app LOGIN PASSWORD 'tiramisu_app' NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE tiramisu TO tiramisu_app;
GRANT USAGE ON SCHEMA public TO tiramisu_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO tiramisu_app;
