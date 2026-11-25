import json
import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bugfixer.common import BugFixerError
from bugfixer.credentials import resolve, save


class CredentialTests(unittest.TestCase):
    def test_database_scope_and_no_plaintext_password(self):
        with tempfile.TemporaryDirectory() as temp:
            database=Path(temp)/'credentials.sqlite3'
            # Synthetic protector for cross-platform CI; real Windows DPAPI is tested separately.
            def protector(data,decrypt=False):
                return data[::-1] if decrypt else data[::-1]
            with patch('bugfixer.credentials._dpapi',protector):
                save(database,'zentao','zentao','production','http://zen.wckj.com/user-login.html','fixture-user','fixture-password')
                cred=resolve({'source':'zentao','credential_ref':'zentao'},database)
                self.assertEqual(cred.password,'fixture-password')
                self.assertNotIn('fixture-password',repr(cred))
                with self.assertRaises(BugFixerError):
                    resolve({'source':'customer_service','credential_ref':'zentao'},database)
            self.assertNotIn(b'fixture-password',database.read_bytes())
            with contextlib.closing(sqlite3.connect(database)) as conn:
                columns={r[1] for r in conn.execute('PRAGMA table_info(web_credentials)')}
            self.assertIn('password_encrypted',columns)
            self.assertNotIn('password',columns)


if __name__=='__main__':
    unittest.main()
