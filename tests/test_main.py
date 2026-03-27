import unittest
from datetime import date

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

    def test_coupon_period_1_to_10(self) -> None:
        barcode, expiry = main.get_coupon_barcode_and_expiry(date(2026, 3, 5))
        self.assertEqual(barcode, "7123100000145")
        self.assertEqual(expiry, date(2026, 3, 10))

    def test_coupon_period_11_to_20(self) -> None:
        barcode, expiry = main.get_coupon_barcode_and_expiry(date(2026, 3, 15))
        self.assertEqual(barcode, "7123100000152")
        self.assertEqual(expiry, date(2026, 3, 20))

    def test_coupon_period_21_to_month_end(self) -> None:
        barcode, expiry = main.get_coupon_barcode_and_expiry(date(2026, 2, 26))
        self.assertEqual(barcode, "7123100000169")
        self.assertEqual(expiry, date(2026, 2, 28))

    def test_coupon_text_contains_expiry(self) -> None:
        text = main.build_coupon_text(date(2026, 3, 20))
        self.assertIn("⏳ Купон действует до 20.03.2026", text)
        self.assertIn("Дарим вам дополнительную скидку 5%.", text)
        self.assertIn("_⚠ Скидка действует только на товары с белыми ценниками_", text)

    def test_extract_attachment_token_recursive(self) -> None:
        payload = {"result": [{"meta": {"token": "img_token_123"}}]}
        self.assertEqual(main._extract_attachment_token(payload), "img_token_123")

    def test_extract_new_member_user_id_direct(self) -> None:
        payload = {"new_member": {"user_id": 555001}}
        self.assertEqual(main.extract_new_member_user_id(payload), "555001")

    def test_extract_new_member_user_id_from_list(self) -> None:
        payload = {"members": [{"id": 777002}]}
        self.assertEqual(main.extract_new_member_user_id(payload), "777002")


if __name__ == "__main__":
    unittest.main()
