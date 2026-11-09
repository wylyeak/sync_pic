-- Run manually as a database administrator after replacing both passwords.
-- Do not save real passwords back into this tracked example.
CREATE DATABASE IF NOT EXISTS picsync CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'picsync'@'%' IDENTIFIED BY 'REPLACE_CONTROLLER_PASSWORD';
GRANT ALL PRIVILEGES ON picsync.* TO 'picsync'@'%';
CREATE USER 'picsync_reader'@'%' IDENTIFIED BY 'REPLACE_DASHBOARD_PASSWORD';
GRANT SELECT ON picsync.* TO 'picsync_reader'@'%';
