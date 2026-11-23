#!/bin/sh
# Seeds an empty database with db/init/*, in name order, the same way the
# postgres image does on first start (docker-compose.yml mounts db/init as
# /docker-entrypoint-initdb.d): *.sql through psql, *.sh run as scripts.
#
# Used by CI: a GitHub Actions service container starts before the repository
# is checked out, so it cannot mount db/init itself.
#
# Needs psql on the PATH and these environment variables:
#   PGHOST, PGPORT, PGPASSWORD                     where to connect
#   POSTGRES_USER, POSTGRES_DB, MCP_READONLY_PASSWORD   used by the scripts
set -eu

cd "$(dirname "$0")/init"
for f in *; do
    case "$f" in
        *.sql)
            echo "applying $f"
            psql -v ON_ERROR_STOP=1 --no-password --quiet \
                 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file "$f"
            ;;
        *.sh)
            echo "running $f"
            sh "$f"
            ;;
        *)
            echo "ignoring $f"
            ;;
    esac
done
