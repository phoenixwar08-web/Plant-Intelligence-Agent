"""Fixed-role local gsql access for the plant Agent.

This module deliberately exposes roles, not arbitrary user names or connection
parameters. Credential files are provisioned by the production deployment;
their contents are used only as gsql pipeline input and are never logged or
accepted from a request.
"""

from __future__ import annotations

import os
import stat
import subprocess
from enum import Enum
from pathlib import Path
from typing import Dict, List, Mapping, Union


DB_NAME = "soil_data"
DB_HOST = "127.0.0.1"
DB_PORT = 7654
GSQL_BIN = "/usr/local/opengauss/bin/gsql"
GSQL_LIBRARY_DIR = "/usr/local/opengauss/lib"
SECRET_DIR = Path("/root/.plant_agent_secrets")


class DatabaseRole(str, Enum):
    READER = "plant_agent_reader"
    HUMAN_EVENT_WRITER = "plant_human_event_writer"


class DatabaseAccessError(RuntimeError):
    """A deliberately non-diagnostic database access failure."""


def _parse_rows(stdout: str) -> List[List[str]]:
    return [line.strip().split("|") for line in stdout.splitlines() if line.strip()]


class GsqlAccess:
    """Run internal SQL with one of the two fixed, least-privileged roles."""

    def __init__(self, credential_files: Mapping[DatabaseRole, Path] | None = None):
        self.credential_files: Dict[DatabaseRole, Path] = dict(credential_files or {
            DatabaseRole.READER: SECRET_DIR / "plant_agent_reader.pgpass",
            DatabaseRole.HUMAN_EVENT_WRITER: SECRET_DIR / "plant_human_event_writer.pgpass",
        })

    @staticmethod
    def _credential_file(role: DatabaseRole, candidate: Path) -> Path:
        """Accept only a root-owned 0700 parent with a root-owned 0600 file."""
        try:
            directory_metadata = candidate.parent.lstat()
            metadata = candidate.lstat()
        except OSError as exc:
            raise DatabaseAccessError("database credentials unavailable") from exc
        expected_owner = os.geteuid()
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
            or directory_metadata.st_uid != expected_owner
            or candidate.parent.is_symlink()
        ):
            raise DatabaseAccessError("database credentials unavailable")
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != expected_owner
            or candidate.is_symlink()
        ):
            raise DatabaseAccessError("database credentials unavailable")
        return candidate

    @staticmethod
    def _password_from_credential(role: DatabaseRole, credential_file: Path) -> str:
        """Read one fixed-format root-only credential without exposing it."""
        try:
            lines = credential_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise DatabaseAccessError("database credentials unavailable") from exc
        if len(lines) != 1:
            raise DatabaseAccessError("database credentials unavailable")
        fields = lines[0].split(":")
        if (
            len(fields) != 5
            or fields[:4] != [DB_HOST, str(DB_PORT), DB_NAME, role.value]
            or not fields[4]
            or any(character.isspace() for character in fields[4])
        ):
            raise DatabaseAccessError("database credentials unavailable")
        return fields[4]

    def execute(self, role: Union[DatabaseRole, str], sql: str, *, timeout: int = 10) -> List[List[str]]:
        if not isinstance(role, DatabaseRole):
            raise DatabaseAccessError("database role is not permitted")
        credential_file = self._credential_file(role, self.credential_files.get(role, Path("/nonexistent")))
        password = self._password_from_credential(role, credential_file)
        environment = os.environ.copy()
        inherited_library_path = environment.get("LD_LIBRARY_PATH", "")
        environment["LD_LIBRARY_PATH"] = GSQL_LIBRARY_DIR + (
            ":" + inherited_library_path if inherited_library_path else ""
        )
        command = [
            GSQL_BIN, "-2", "-v", "ON_ERROR_STOP=1", "-h", DB_HOST, "-p", str(DB_PORT), "-d", DB_NAME,
            "-U", role.value, "-t", "-A", "-F", "|", "-c", sql.strip(),
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout,
                check=False, env=environment, input=password + "\n",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DatabaseAccessError("database operation failed") from exc
        if result.returncode != 0:
            raise DatabaseAccessError("database operation failed")
        return _parse_rows(result.stdout)


_ACCESS = GsqlAccess()


def least_privilege_enabled() -> bool:
    """Deployment switch. Anything other than an explicit 1 keeps cutover off."""
    return os.environ.get("PLANT_AGENT_DB_LEAST_PRIVILEGE") == "1"


def run_reader_sql(sql: str) -> List[List[str]]:
    return _ACCESS.execute(DatabaseRole.READER, sql, timeout=10)


def run_human_event_writer_sql(sql: str) -> List[List[str]]:
    return _ACCESS.execute(DatabaseRole.HUMAN_EVENT_WRITER, sql, timeout=15)
