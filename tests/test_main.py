import unittest

import main


class TestMainHelpers(unittest.TestCase):
    def test_extract_user_id_from_message_sender(self) -> None:
        payload = {"message": {"sender": {"user_id": 12345}}}
        self.assertEqual(main.extract_user_id(payload), "12345")

    def test_extract_chat_id_from_recipient(self) -> None:
        payload = {"message": {"recipient": {"chat_id": 777}}}
        self.assertEqual(main.extract_chat_id(payload), "777")

    def test_extract_message_text_from_body(self) -> None:
        payload = {"message": {"body": {"text": "Тест"}}}
        self.assertEqual(main.extract_message_text(payload), "Тест")

    def test_normalize_incoming_text_with_mention(self) -> None:
        self.assertEqual(main.normalize_incoming_text("/id@my_bot"), "/id")

    def test_extract_dedup_key_from_mid(self) -> None:
        payload = {"update_type": "message_created", "message": {"body": {"mid": "m-1"}}}
        self.assertEqual(main.extract_dedup_key(payload), "message_created:mid:m-1")

    def test_extract_dedup_key_from_callback(self) -> None:
        payload = {"update_type": "message_callback", "callback": {"callback_id": "cb-1"}}
        self.assertEqual(main.extract_dedup_key(payload), "message_callback:cb:cb-1")


if __name__ == "__main__":
    unittest.main()
