import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class DatabaseInitTests(unittest.TestCase):
    def run_init(self, user='reader'):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            client = root/'mysql'
            client.write_text('#!/bin/sh\ncat > "$TEST_SQL"\nprintf "%s\\n" "$@" > "$TEST_ARGS"\n')
            client.chmod(0o755)
            password = "quote'and\\slash$()"
            env = dict(os.environ, PATH=d+os.pathsep+os.environ['PATH'],
                       MYSQL_PASSWORD='controller-test', MYSQL_ROOT_PASSWORD='root-test',
                       MYSQL_DATABASE='picsync_test', MYSQL_USER='writer',
                       PICSYNC_DASHBOARD_MYSQL_USER=user,
                       PICSYNC_DASHBOARD_MYSQL_PASSWORD=password,
                       TEST_SQL=str(root/'sql'), TEST_ARGS=str(root/'args'))
            script = Path(__file__).resolve().parents[1]/'deploy/mysql-init.sh'
            result = subprocess.run(['sh', str(script)], env=env, capture_output=True, text=True)
            sql = (root/'sql').read_text() if (root/'sql').exists() else ''
            args = (root/'args').read_text() if (root/'args').exists() else ''
            return result, sql, args, password

    def test_reader_gets_select_only_and_password_is_not_a_cli_argument(self):
        result, sql, args, password = self.run_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(password.encode().hex(), sql)
        self.assertIn("GRANT SELECT ON `picsync_test`.* TO 'reader'@'%'", sql)
        self.assertNotIn('GRANT ALL', sql)
        self.assertNotIn(password, args+result.stdout+result.stderr)
        self.assertNotIn('root-test', args)

    def test_invalid_or_shared_account_is_rejected_before_sql(self):
        for user in ('bad;DROP', 'writer'):
            result, sql, _, _ = self.run_init(user)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sql, '')
