import unittest
from datetime import date
from unittest.mock import patch

import main


class TestMainHelpers(unittest.TestCase):
    class _Resp:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self._payload = payload or {}
            self.content = b"{}"
            self.text = ""

        def json(self):
            return self._payload

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

    def test_effective_update_types_contains_base_types(self) -> None:
        update_types = main.get_effective_update_types()
        self.assertIn("message_created", update_types)
        self.assertIn("bot_started", update_types)

    def test_get_channel_id_candidates_contains_signed_and_unsigned(self) -> None:
        candidates = main.get_channel_id_candidates()
        self.assertIn("-72559954357735", candidates)
        self.assertIn("72559954357735", candidates)

    def test_miniapp_url_from_webhook_url(self) -> None:
        original = main.MAX_WEBHOOK_URL
        try:
            main.MAX_WEBHOOK_URL = "https://example.com/webhook"
            self.assertEqual(main.get_miniapp_url(), "https://example.com/miniapp")
        finally:
            main.MAX_WEBHOOK_URL = original

    def test_render_miniapp_contains_show_coupon_button(self) -> None:
        html = main.render_miniapp_html()
        self.assertIn("Показать купон", html)
        self.assertNotIn('placeholder="Введите user_id"', html)
        self.assertIn("initDataUnsafe", html)
        self.assertIn("max-web-app.js", html)
        self.assertIn("https://max.ru/id344309962847_biz", html)
        self.assertIn("https://web.max.ru/-72559954357735", html)
        self.assertIn("window.WebApp.openLink(deepLink)", html)
        self.assertIn("window.location.assign(deepLink)", html)
        self.assertNotIn("setTimeout(fallbackToWeb", html)
        self.assertIn("/miniapp/start-subscribe-watch", html)

    def test_build_miniapp_button_attachments_uses_open_app(self) -> None:
        original_web_app = main.MAX_WEB_APP
        original_webhook_url = main.MAX_WEBHOOK_URL
        try:
            main.MAX_WEB_APP = "my_test_bot"
            main.MAX_WEBHOOK_URL = "https://example.com/webhook"
            attachments = main.build_miniapp_button_attachments()
        finally:
            main.MAX_WEB_APP = original_web_app
            main.MAX_WEBHOOK_URL = original_webhook_url

        self.assertEqual(attachments[0]["type"], "inline_keyboard")
        button = attachments[0]["payload"]["buttons"][0][0]
        self.assertEqual(button["type"], "open_app")
        self.assertEqual(button["text"], "Получить купон")
        self.assertEqual(button["web_app"], "my_test_bot")

    def test_contains_user_id_recursive(self) -> None:
        payload = {"items": [{"user": {"id": 123}}, {"meta": "x"}]}
        self.assertTrue(main.contains_user_id(payload, "123"))
        self.assertFalse(main.contains_user_id(payload, "999"))

    def test_is_subscription_confirmed_by_flag(self) -> None:
        self.assertTrue(main.is_subscription_confirmed({"is_subscribed": True}, "1"))
        self.assertTrue(main.is_subscription_confirmed({"member": "joined"}, "1"))
        self.assertTrue(main.is_subscription_confirmed({"status": "subscriber"}, "1"))

    def test_get_user_subscription_state_unknown_on_api_errors(self) -> None:
        original_token = main.MAX_BOT_TOKEN
        try:
            main.MAX_BOT_TOKEN = "test-token"
            with patch("main.requests.get", return_value=self._Resp(400)):
                self.assertEqual(main.get_user_subscription_state("1"), "unknown")
        finally:
            main.MAX_BOT_TOKEN = original_token

if __name__ == "__main__":
    unittest.main()
