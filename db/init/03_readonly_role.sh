#!/bin/sh
# Creates the role the MCP server connects as. It is the first line of
# defence: even if every check in the application were bypassed, this role
# simply has no privilege to change anything.
#
# Runs once, on first start of an empty data directory, as the superuser
# configured by POSTGRES_USER (see docker-compose.yml).
set -eu

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v ro_password="$MCP_READONLY_PASSWORD" \
     -v db_name="$POSTGRES_DB" <<'EOSQL'

-- A login role with no special attributes: not a superuser, cannot create
-- databases or roles, cannot bypass row-level security, cannot replicate.
CREATE ROLE mcp_readonly
    LOGIN PASSWORD :'ro_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 10;

-- Server-side defaults for every session of this role. The application also
-- sets these per transaction; these are the backstop if it ever forgets.
ALTER ROLE mcp_readonly SET default_transaction_read_only = on;
ALTER ROLE mcp_readonly SET statement_timeout = '10s';
ALTER ROLE mcp_readonly SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE mcp_readonly SET search_path = shop;

-- By default every role (PUBLIC) may connect and create temp tables in any
-- database. Take that away and grant back only CONNECT.
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO mcp_readonly;

-- Nothing in the public schema, read access to the shop schema only.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA shop TO mcp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA shop TO mcp_readonly;

-- Deliberately no ALTER DEFAULT PRIVILEGES: a table added later is not
-- exposed to agents until someone grants it on purpose.
EOSQL
