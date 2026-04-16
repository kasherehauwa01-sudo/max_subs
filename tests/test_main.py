import unittest
from datetime import date
from unittest.mock import patch
from datetime import datetime, timezone

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

    def test_has_qr_utm_source_detects_payload(self) -> None:
        payload = {"message": {"body": {"text": "/start utm_source=qr_podpiska"}}}
        self.assertTrue(main.has_qr_utm_source(payload))

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
        self.assertIn("Максимальная суммарная скидка - 20%", text)
        self.assertIn("Купон доступен к получению один раз", text)
        self.assertIn("_⚠ Скидка действует только на товары с белыми ценниками.", text)

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

    def test_get_channel_api_targets_contains_channel_slug(self) -> None:
        targets = main.get_channel_api_targets()
        self.assertIn("id344309962847_biz", targets)

    def test_miniapp_url_from_webhook_url(self) -> None:
        original = main.MAX_WEBHOOK_URL
        try:
            main.MAX_WEBHOOK_URL = "https://example.com/webhook"
            self.assertEqual(main.get_miniapp_url(), "https://example.com/miniapp")
        finally:
            main.MAX_WEBHOOK_URL = original

    def test_render_miniapp_contains_only_subscribe_button(self) -> None:
        html = main.render_miniapp_html()
        self.assertIn("Подпишись на канал и получи доп.скидку -5%", html)
        self.assertNotIn("Проверить подписку", html)
        self.assertNotIn('id="showCouponBtn"', html)
        self.assertNotIn('placeholder="Введите user_id"', html)
        self.assertIn("initDataUnsafe", html)
        self.assertIn("max-web-app.js", html)
        self.assertIn("https://max.ru/id344309962847_biz", html)
        self.assertIn("https://web.max.ru/-72559954357735", html)
        self.assertIn("window.WebApp.openLink(deepLink)", html)
        self.assertIn("window.location.assign(deepLink)", html)
        self.assertNotIn("setTimeout(fallbackToWeb", html)
        self.assertIn("/miniapp/start-subscribe-watch", html)
        self.assertIn("/miniapp/participation", html)
        self.assertIn("Участие в акции «Скидка за подписку» возможно только один раз.", html)

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

    def test_render_dashboard_html_contains_controls(self) -> None:
        html = main.render_dashboard_html()
        self.assertIn("Статистика отправленных купонов", html)
        self.assertIn("Ручной выбор периода", html)
        self.assertIn("По дням", html)
        self.assertIn("/dashboard/data", html)
        self.assertIn("user_id", html)

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

    def test_subscribe_click_watcher_sends_coupon_after_delay(self) -> None:
        with patch("main.send_coupon") as send_coupon_mock, patch("main.time.sleep", return_value=None) as sleep_mock:
            main._send_coupon_after_subscribe_click("123")
            sleep_mock.assert_called_once_with(6.0)
            send_coupon_mock.assert_called_once_with(user_id="123", chat_id=None)

    def test_log_coupon_event_to_google_sheet_disabled(self) -> None:
        original = main.GOOGLE_SHEETS_ENABLED
        try:
            main.GOOGLE_SHEETS_ENABLED = False
            main.log_coupon_event_to_google_sheet("123")
        finally:
            main.GOOGLE_SHEETS_ENABLED = original

    def test_normalize_service_account_info_private_key_newlines(self) -> None:
        raw = {"private_key": "-----BEGIN PRIVATE KEY-----\\nABC\\n-----END PRIVATE KEY-----\\n"}
        normalized = main.normalize_service_account_info(raw)
        self.assertIn("\nABC\n", normalized["private_key"])

    def test_parse_google_service_account_raw_json_in_quotes(self) -> None:
        value = '"{\\"type\\": \\"service_account\\", \\"project_id\\": \\"p\\", \\"private_key\\": \\"k\\", \\"client_email\\": \\"a@b\\", \\"token_uri\\": \\"https://oauth2.googleapis.com/token\\"}"'
        parsed = main.parse_google_service_account(value)
        self.assertEqual(parsed["type"], "service_account")
        self.assertEqual(parsed["project_id"], "p")

    def test_get_google_sheets_config_issues_parse_error(self) -> None:
        original_enabled = main.GOOGLE_SHEETS_ENABLED
        original_sa = main.GOOGLE_SERVICE_ACCOUNT_JSON
        try:
            main.GOOGLE_SHEETS_ENABLED = True
            main.GOOGLE_SERVICE_ACCOUNT_JSON = "not-json-and-not-path"
            issues = main.get_google_sheets_config_issues()
            self.assertTrue(any("parse error" in issue for issue in issues))
        finally:
            main.GOOGLE_SHEETS_ENABLED = original_enabled
            main.GOOGLE_SERVICE_ACCOUNT_JSON = original_sa

    def test_send_coupon_only_once_per_user(self) -> None:
        main._coupon_sent_users.clear()
        with (
            patch("main.log_coupon_event_to_google_sheet") as log_mock,
            patch("main.generate_ean13_png_file", return_value="dummy.png"),
            patch("main.upload_image_and_get_token", return_value="token-1"),
            patch("main.send_max_message") as send_mock,
            patch("main.TemporaryDirectory") as temp_dir_mock,
        ):
            temp_dir_mock.return_value.__enter__.return_value = "/tmp"
            temp_dir_mock.return_value.__exit__.return_value = False

            main.send_coupon(user_id="111", chat_id=None)
            main.send_coupon(user_id="111", chat_id=None)

            self.assertEqual(log_mock.call_count, 1)
            self.assertEqual(send_mock.call_count, 2)
            self.assertIn("уже получили купон", send_mock.call_args_list[1].kwargs["text"].lower())

    def test_send_coupon_allow_duplicate_for_special_user(self) -> None:
        main._coupon_sent_users.clear()
        with (
            patch("main.log_coupon_event_to_google_sheet") as log_mock,
            patch("main.generate_ean13_png_file", return_value="dummy.png"),
            patch("main.upload_image_and_get_token", return_value="token-1"),
            patch("main.send_max_message") as send_mock,
            patch("main.TemporaryDirectory") as temp_dir_mock,
        ):
            temp_dir_mock.return_value.__enter__.return_value = "/tmp"
            temp_dir_mock.return_value.__exit__.return_value = False

            main.send_coupon(user_id="24324984", chat_id=None)
            main.send_coupon(user_id="24324984", chat_id=None)

            self.assertEqual(log_mock.call_count, 2)
            self.assertEqual(send_mock.call_count, 2)

    def test_miniapp_participation_blocks_regular_user(self) -> None:
        with patch("main.get_coupon_participation_date", return_value="10.04.2026"):
            response = main.miniapp_participation("100")
            payload = response.body.decode("utf-8")
            self.assertIn('"already_participated":true', payload)
            self.assertIn('"participation_date":"10.04.2026"', payload)

    def test_miniapp_participation_allows_special_user(self) -> None:
        with patch("main.get_coupon_participation_date", return_value="10.04.2026"):
            response = main.miniapp_participation("24324984")
            payload = response.body.decode("utf-8")
            self.assertIn('"already_participated":false', payload)

    def test_process_update_no_auto_reply_when_user_id_missing(self) -> None:
        payload = {"update_type": "message_created", "message": {"body": {"text": "hello"}}}
        with patch("main.send_max_message") as send_mock:
            main.process_update(payload)
            send_mock.assert_not_called()

    def test_aggregate_dates_day_week_month(self) -> None:
        dates = [date(2026, 4, 1), date(2026, 4, 1), date(2026, 4, 8)]
        self.assertEqual(main.aggregate_dates(dates, "day")["2026-04-01"], 2)
        self.assertIn("2026-W14", main.aggregate_dates(dates, "week"))
        self.assertEqual(main.aggregate_dates(dates, "month")["2026-04"], 3)

    def test_resolve_period_dates_custom(self) -> None:
        start, end = main.resolve_period_dates("custom", "2026-04-01", "2026-04-10", datetime(2026, 4, 14, tzinfo=timezone.utc))
        self.assertEqual(start, date(2026, 4, 1))
        self.assertEqual(end, date(2026, 4, 10))

    def test_process_update_dashboard_command_for_allowed_user(self) -> None:
        payload = {"update_type": "message_created", "message": {"sender": {"user_id": "242649311"}, "body": {"text": "Дашборд"}}}
        with patch("main.send_dashboard_entry") as dashboard_mock:
            main.process_update(payload)
            dashboard_mock.assert_called_once()

    def test_process_update_start_with_qr_utm_sends_qr_button(self) -> None:
        main._qr_podpiska_users.clear()
        payload = {
            "update_type": "message_created",
            "message": {"sender": {"user_id": "100"}, "body": {"text": "/start utm_source=qr_podpiska"}},
        }
        with patch("main.send_qr_subscription_entry") as qr_mock:
            main.process_update(payload)
            qr_mock.assert_called_once_with(user_id="100", chat_id=None)

    def test_process_update_qr_callback_starts_watcher(self) -> None:
        payload = {
            "update_type": "message_callback",
            "message": {"sender": {"user_id": "100"}},
            "callback": {"payload": "qr_subscribe_coupon"},
        }
        with patch("main.start_subscription_watch", return_value=True) as watch_mock, patch("main.send_max_message") as send_mock:
            main.process_update(payload)
            watch_mock.assert_called_once_with("100")
            send_mock.assert_called_once()

    def test_process_update_dashboard_command_for_second_allowed_user(self) -> None:
        payload = {"update_type": "message_created", "message": {"sender": {"user_id": "24324984"}, "body": {"text": "Статистика"}}}
        with patch("main.send_dashboard_entry") as dashboard_mock:
            main.process_update(payload)
            dashboard_mock.assert_called_once()

    def test_process_update_dashboard_command_ignored_for_other_user(self) -> None:
        payload = {"update_type": "message_created", "message": {"sender": {"user_id": "777"}, "body": {"text": "Статистика"}}}
        with patch("main.send_dashboard_entry") as dashboard_mock, patch("main.send_max_message") as send_mock:
            main.process_update(payload)
            dashboard_mock.assert_not_called()
            send_mock.assert_not_called()

    def test_process_update_id_command_disabled(self) -> None:
        payload = {"update_type": "message_created", "message": {"sender": {"user_id": "777"}, "body": {"text": "id"}}}
        with patch("main.send_max_message") as send_mock:
            main.process_update(payload)
            send_mock.assert_called_once_with(
                text="Функция отправки user_id отключена.",
                user_id="777",
                chat_id=None,
            )

    def test_is_dashboard_user_allowed(self) -> None:
        self.assertTrue(main.is_dashboard_user_allowed("242649311"))
        self.assertTrue(main.is_dashboard_user_allowed("24324984"))
        self.assertFalse(main.is_dashboard_user_allowed("777"))

    def test_should_send_monthly_reminder_only_on_29(self) -> None:
        self.assertTrue(main.should_send_monthly_reminder(datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)))
        self.assertFalse(main.should_send_monthly_reminder(datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc)))

    def test_send_monthly_reminder_if_needed_once_per_day(self) -> None:
        main._last_monthly_reminder_date = None
        now = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        with patch("main.send_max_message") as send_mock:
            first = main.send_monthly_reminder_if_needed(now)
            second = main.send_monthly_reminder_if_needed(now)
            self.assertTrue(first)
            self.assertFalse(second)
            send_mock.assert_called_once_with(
                text="Обновить штрихкоды в 1с",
                user_id="24324984",
                chat_id=None,
            )

    def test_send_monthly_reminder_if_needed_not_29(self) -> None:
        main._last_monthly_reminder_date = None
        now = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
        with patch("main.send_max_message") as send_mock:
            sent = main.send_monthly_reminder_if_needed(now)
            self.assertFalse(sent)
            send_mock.assert_not_called()

if __name__ == "__main__":
    unittest.main()
