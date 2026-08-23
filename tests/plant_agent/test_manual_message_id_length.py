import unittest

from services.human_record_gateway import RecordContext, validate_context


class QQMessageIdLengthTests(unittest.TestCase):
    def test_accepts_realistic_qq_message_id_longer_than_128_characters(self):
        context = RecordContext(
            channel="qqbot",
            agent_id="qqbot4",
            conversation_id="group-1",
            sender_id="sender-1",
            sender_name="Tester",
            source_message_id="R" * 137,
        )

        validated = validate_context(context)

        self.assertEqual(validated.source_message_id, "R" * 137)


if __name__ == "__main__":
    unittest.main()
