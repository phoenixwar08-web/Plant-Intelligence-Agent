"""Stage 3 must not retain an application fallback to the DB administrator."""

import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


class NoLegacyDatabaseEscalationTests(unittest.TestCase):
    def test_business_database_paths_do_not_escalate_to_opengauss(self):
        for relative_path in ("plant_state_builder.py", "services/human_record_gateway.py"):
            source = (PROJECT_DIR / relative_path).read_text(encoding="utf-8-sig")
            self.assertNotIn('"su", "-", "opengauss"', source)
            self.assertNotIn("su - opengauss", source)


if __name__ == "__main__":
    unittest.main()
