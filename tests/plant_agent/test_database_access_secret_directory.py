"""Regression coverage for the root-only credential directory boundary."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from services.database_access import DatabaseAccessError, DatabaseRole, GsqlAccess


class CredentialDirectoryTests(unittest.TestCase):
    def test_uses_fixed_absolute_gsql_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "reader.pgpass"
            secret.write_text("127.0.0.1:7654:soil_data:plant_agent_reader:x\n", encoding="utf-8")
            secret.chmod(0o600)
            access = GsqlAccess({DatabaseRole.READER: secret})
            with patch("services.database_access.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "1\\n"
                access.execute(DatabaseRole.READER, "SELECT 1")
            self.assertEqual(run.call_args.args[0][0], "/usr/local/opengauss/bin/gsql")
            self.assertTrue(run.call_args.kwargs["env"]["LD_LIBRARY_PATH"].startswith("/usr/local/opengauss/lib"))
            self.assertIn("-2", run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["input"], "x\n")

    def test_rejects_credential_in_group_or_world_accessible_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_dir = Path(directory)
            secret_dir.chmod(0o755)
            credential = secret_dir / "reader.pgpass"
            credential.write_text("x", encoding="utf-8")
            credential.chmod(0o600)
            access = GsqlAccess({DatabaseRole.READER: credential})

            with patch("services.database_access.subprocess.run") as run:
                with self.assertRaises(DatabaseAccessError):
                    access.execute(DatabaseRole.READER, "SELECT 1")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
