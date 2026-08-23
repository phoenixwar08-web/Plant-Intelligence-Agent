import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
from analytics import data_quality_auditor as auditor

NOW = datetime.fromisoformat('2026-07-19T08:00:00+08:00')

class DataQualityAuditorTests(unittest.TestCase):
    def test_build_reports_stale_sensor_and_malformed_vision_line(self):
        status={'generated_at':'2026-07-19T07:55:00+08:00','sensor':{'recv_time':'2026-07-19 07:40:00'},'visual':{'observed_at':'2026-07-18T06:00:00+08:00'}}
        decision={'generated_at':'2026-07-19T07:55:00+08:00'}; trend={'generated_at':'2026-07-19T07:59:00+08:00'}
        phase={'irrigation_style_experiment':{'updated_at':NOW.timestamp()}}
        lines=['{"device_code":"soil2","observed_at":"2026-07-19T07:00:00+08:00","visual":{}}','not-json']
        with patch.object(auditor,'_load_json',side_effect=[status,decision,trend,phase]), patch.object(auditor,'_read_lines',return_value=lines), patch.object(auditor,'_image_mtime',return_value=NOW):
            report=auditor.build_quality_report(now=NOW)
        self.assertEqual(report['freshness']['sensor']['state'],'stale')
        self.assertEqual(report['history_integrity']['vision_jsonl']['malformed_lines'],1)
        self.assertEqual(report['overall'],'degraded')
    def test_write_appends_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(auditor,'REPORT_PATH',root/'report.json'), patch.object(auditor,'HISTORY_PATH',root/'history.jsonl'), patch.object(auditor,'build_quality_report',return_value={'schema_version':1,'device_code':'soil2'}):
                auditor.write_quality_report(); auditor.write_quality_report()
            self.assertEqual(len((root/'history.jsonl').read_text().splitlines()),2)

if __name__=='__main__': unittest.main()
