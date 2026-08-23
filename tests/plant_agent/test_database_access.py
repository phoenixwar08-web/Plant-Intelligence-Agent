"""Regression tests for the least-privilege gsql access boundary."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from services.database_access import DatabaseAccessError, DatabaseRole, GsqlAccess, least_privilege_enabled


class DatabaseAccessTests(unittest.TestCase):
    def test_cutover_switch_requires_an_explicit_one(self):
        with patch.dict(os.environ, {"PLANT_AGENT_DB_LEAST_PRIVILEGE": "0"}, clear=False):
            self.assertFalse(least_privilege_enabled())
        with patch.dict(os.environ, {"PLANT_AGENT_DB_LEAST_PRIVILEGE": "1"}, clear=False):
            self.assertTrue(least_privilege_enabled())

    def test_reader_uses_only_reader_role_and_pipeline_password(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "reader.pgpass"
            secret.write_text("127.0.0.1:7654:soil_data:plant_agent_reader:secret\n", encoding="utf-8")
            secret.chmod(0o600)
            access = GsqlAccess({DatabaseRole.READER: secret})
            with patch("services.database_access.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "ok\n"
                run.return_value.stderr = ""
                self.assertEqual(access.execute(DatabaseRole.READER, "SELECT 1"), [["ok"]])
            args, kwargs = run.call_args
            self.assertIn("plant_agent_reader", args[0])
            self.assertIn("-2", args[0])
            self.assertIn("ON_ERROR_STOP=1", args[0])
            self.assertNotIn("su", args[0])
            self.assertEqual(kwargs["input"], "secret\n")
            self.assertNotIn("PGPASSFILE", kwargs["env"])

    def test_writer_cannot_be_selected_by_untrusted_role_name(self):
        access = GsqlAccess({})
        with self.assertRaises(DatabaseAccessError):
            access.execute("opengauss", "SELECT 1")

    def test_rejects_missing_or_insecure_credential_file(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "reader.pgpass"
            secret.write_text("x", encoding="utf-8")
            secret.chmod(0o644)
            access = GsqlAccess({DatabaseRole.READER: secret})
            with self.assertRaises(DatabaseAccessError):
                access.execute(DatabaseRole.READER, "SELECT 1")

    def test_database_failure_does_not_include_password_file_or_sql(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "reader.pgpass"
            secret.write_text("127.0.0.1:7654:soil_data:plant_agent_reader:secret\n", encoding="utf-8")
            secret.chmod(0o600)
            access = GsqlAccess({DatabaseRole.READER: secret})
            with patch("services.database_access.subprocess.run") as run:
                run.return_value.returncode = 1
                run.return_value.stdout = "internal database error"
                run.return_value.stderr = ""
                with self.assertRaisesRegex(DatabaseAccessError, "database operation failed") as raised:
                    access.execute(DatabaseRole.READER, "SELECT private_value")
            self.assertNotIn(str(secret), str(raised.exception))
            self.assertNotIn("private_value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
