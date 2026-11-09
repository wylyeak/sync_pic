#!/bin/sh
# First initialization only. No passwords in command arguments or SQL log output.
set -eu
: "${MYSQL_PASSWORD:?Set PICSYNC_MYSQL_PASSWORD}"
: "${PICSYNC_DASHBOARD_MYSQL_PASSWORD:?Set PICSYNC_DASHBOARD_MYSQL_PASSWORD}"
for identifier in "$MYSQL_DATABASE" "$MYSQL_USER" "$PICSYNC_DASHBOARD_MYSQL_USER"; do
    case "$identifier" in ''|*[!A-Za-z0-9_]*) echo 'Database/user names must use letters, digits and underscores' >&2; exit 1 ;; esac
    [ "${#identifier}" -le 64 ] || exit 1
done
[ "$MYSQL_USER" != "$PICSYNC_DASHBOARD_MYSQL_USER" ] || { echo 'Use separate controller and reader users' >&2; exit 1; }
password_hex=$(printf '%s' "$PICSYNC_DASHBOARD_MYSQL_PASSWORD" | od -An -tx1 | tr -d ' \n')
export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
mysql --protocol=socket -uroot <<SQL
SET @reader_password = CONVERT(0x$password_hex USING utf8mb4);
SET @create_reader = CONCAT('CREATE USER IF NOT EXISTS ''$PICSYNC_DASHBOARD_MYSQL_USER''@''%'' IDENTIFIED BY ', QUOTE(@reader_password));
PREPARE reader_statement FROM @create_reader;
EXECUTE reader_statement;
DEALLOCATE PREPARE reader_statement;
GRANT SELECT ON \`$MYSQL_DATABASE\`.* TO '$PICSYNC_DASHBOARD_MYSQL_USER'@'%';
SQL
