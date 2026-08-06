import unittest
from datetime import datetime

from app.schemas.dtos import ConversationSummaryResponse


class DatetimeSerializationTests(unittest.TestCase):
    def test_naive_database_utc_datetime_is_serialized_with_utc_marker(self):
        response = ConversationSummaryResponse(
            sessionId="session",
            title="会话",
            lastMessage="内容",
            createdAt=datetime(2026, 7, 29, 5, 46),
            updatedAt=datetime(2026, 7, 29, 5, 46),
        )

        payload = response.model_dump_json()

        self.assertIn('"createdAt":"2026-07-29T05:46:00Z"', payload)
        self.assertIn('"updatedAt":"2026-07-29T05:46:00Z"', payload)


if __name__ == "__main__":
    unittest.main()
