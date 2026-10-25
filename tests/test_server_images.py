"""Image workflows use isolated temporary data and mocked providers only."""
import copy
from datetime import datetime, timedelta
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import App, Handler, InputError, ThreadingHTTPServer, TZ, stamp, vision_settings
from vision_engine import VisionError


RAW_IMAGE = "TEST-ONLY-IMAGE-BLOB-NEVER-PERSIST"
PREPARED = {"data_url": "data:image/png;base64,TEST-ONLY-PREPARED-BLOB-NEVER-PERSIST",
            "mime": "image/png", "width": 200, "height": 100, "sha256": "a" * 64}
GLUCOSE = {"category": "glucose_meter", "readings": [{"kind": "glucose", "value": 126, "unit": "mg/dL"}],
           "summary": "待核对血糖 126 mg/dL", "warnings": [], "device_time_text": "10/24 08:30"}
BP = {"category": "bp_meter", "readings": [{"kind": "bp", "systolic": 135, "diastolic": 85, "unit": "mmHg"}],
      "summary": "待核对血压 135/85 mmHg", "warnings": []}


class ImageFixture:
    def start_app(self):
        self.temp = tempfile.TemporaryDirectory(prefix="family-image-test-")
        self.app = App(self.temp.name)
        self.preparer_patch = patch("server.vision_engine.prepare_image", return_value=copy.deepcopy(PREPARED))
        self.recognizer_patch = patch("server.vision_engine.recognize", side_effect=lambda *_: copy.deepcopy(GLUCOSE))
        self.preparer = self.preparer_patch.start()
        self.recognizer = self.recognizer_patch.start()

    def stop_app(self):
        self.recognizer_patch.stop()
        self.preparer_patch.stop()
        self.app.db.close()
        self.temp.cleanup()

    def enable_vision(self):
        return self.app.update_config({"model": {"api_key": "FAKE-TEXT-SECRET", "model": "deepseek-flash"},
                                       "vision": {"enabled": True}})

    def live(self):
        self.app.update_config({"wechat": {"group_name": "图片测试群", "patient_sender": "爸爸", "caregiver_sender": "家属"}})
        self.app.bridge_heartbeat({"status": "dry_run"})
        self.app.update_config({"wechat": {"live_enabled": True}})
        self.app.bridge_heartbeat({"status": "ready"})

    def image(self, **kwargs):
        return self.app.process_image(RAW_IMAGE, **kwargs)

    def assert_no_images_persisted(self):
        # Only the test's temporary DB is read. No real user config is accessed.
        persisted = "\n".join(self.app.db.iterdump())
        self.assertNotIn(RAW_IMAGE, persisted)
        self.assertNotIn(PREPARED["data_url"], persisted)
        self.assertNotIn("TEST-ONLY-PREPARED-BLOB", json.dumps(self.app.state()))


class ImageAppTests(ImageFixture, unittest.TestCase):
    def setUp(self):
        self.start_app()

    def tearDown(self):
        self.stop_app()

    def test_config_redacts_both_keys_and_preserves_empty_form_values(self):
        self.app.update_config({"model": {"api_key": "FAKE-TEXT-SECRET"},
                                "vision": {"reuse_text_key": False, "api_key": "FAKE-VISION-SECRET", "enabled": True}})
        response = self.app.update_config({"model": {"api_key": ""}, "vision": {"api_key": ""}})
        public = json.dumps([response, self.app.state()])
        for secret in ("FAKE-TEXT-SECRET", "FAKE-VISION-SECRET"):
            self.assertNotIn(secret, public)
        self.assertTrue(response["config"]["vision"]["has_key"])
        self.assertTrue(response["config"]["vision"]["has_effective_key"])
        self.assertEqual(vision_settings(self.app.config)["api_key"], "FAKE-VISION-SECRET")

    def test_reuse_requires_same_origin_including_port_but_allows_path(self):
        self.enable_vision()
        self.app.update_config({"vision": {"base_url": "https://api.deepseek.com:443/v1"}})
        self.assertEqual(vision_settings(self.app.config)["api_key"], "FAKE-TEXT-SECRET")
        for other in ("https://api.example.org", "https://api.deepseek.com:444", "https://api.deepseek.com.evil.example"):
            before = copy.deepcopy(self.app.config)
            with self.subTest(other=other), self.assertRaises(InputError):
                self.app.update_config({"vision": {"base_url": other}})
            self.assertEqual(self.app.config, before)

    def test_explicit_separate_key_allows_different_provider(self):
        self.enable_vision()
        self.app.update_config({"vision": {"base_url": "https://vision.example.org/v1", "model": "test-vision", "reuse_text_key": False,
                                           "api_key": "FAKE-OTHER-KEY", "enabled": True}})
        self.image()
        used_config = self.recognizer.call_args.args[2]
        self.assertEqual(used_config["api_key"], "FAKE-OTHER-KEY")
        self.assertNotEqual(used_config["api_key"], self.app.config["model"]["api_key"])

    def test_deepseek_pro_rejected_without_changing_configuration(self):
        self.enable_vision()
        before = copy.deepcopy(self.app.config)
        with self.assertRaises(InputError):
            self.app.update_config({"vision": {"model": "deepseek-v4-pro"}})
        self.assertEqual(before, self.app.config)

    def test_clear_text_key_disables_reusing_vision(self):
        self.enable_vision()
        response = self.app.update_config({"clear_api_key": True})
        self.assertFalse(response["config"]["vision"]["enabled"])
        self.assertFalse(response["config"]["vision"]["has_effective_key"])
        result = self.image()
        self.assertEqual(result["answer_source"], "vision_error")
        self.recognizer.assert_not_called()

    def test_clear_separate_vision_key_disables_only_vision(self):
        self.enable_vision()
        self.app.update_config({"model": {"enabled": True}, "vision": {"reuse_text_key": False, "api_key": "FAKE-SEPARATE-KEY"}})
        response = self.app.update_config({"clear_vision_api_key": True})
        self.assertFalse(response["config"]["vision"]["enabled"])
        self.assertFalse(response["config"]["vision"]["has_key"])
        self.assertTrue(response["config"]["model"]["enabled"])
        self.assertTrue(response["config"]["model"]["has_key"])

    def test_image_draft_normalizes_mgdl_without_saving_or_guessing_time(self):
        self.enable_vision()
        result = self.image(text="今天空腹")
        item = result["items"][0]
        self.assertEqual((item["value"], item["unit"]), (7.0, "mmol/L"))
        self.assertEqual((item["original_value"], item["original_unit"]), (126, "mg/dL"))
        self.assertIsNone(item["measured_at"])
        self.assertEqual(item["context"], "unknown")
        self.assertTrue(item["requires_confirmation"])
        self.assertFalse(self.app.state()["measurements"])
        pending = self.app.state()["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["image_metadata"]["sha256"], PREPARED["sha256"])
        self.assert_no_images_persisted()

    def test_confirmation_requires_subject_actual_time_and_glucose_context(self):
        self.enable_vision()
        pid = self.image()["pending_id"]
        measured = stamp(datetime.now(TZ) - timedelta(days=2))
        invalid = ({}, {"subject_confirmed": True},
                   {"subject_confirmed": True, "measured_at": measured},
                   {"subject_confirmed": True, "measured_at": measured, "context": "unknown"},
                   {"subject_confirmed": True, "measured_at": stamp(datetime.now(TZ) + timedelta(days=1)), "context": "fasting"})
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(InputError):
                self.app.confirm(pid, **arguments)
            self.assertFalse(self.app.state()["measurements"])
            self.assertEqual(len(self.app.state()["pending"]), 1)
        self.app.confirm(pid, subject_confirmed=True, measured_at=measured, context="postmeal2h")
        saved = self.app.state()["measurements"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["measured_at"], measured)
        self.assertEqual(saved[0]["context"], "postmeal2h")
        self.assertEqual(saved[0]["value"], 7.0)
        self.assertTrue(saved[0]["confirmed"])
        with self.assertRaises(InputError):
            self.app.confirm(pid, subject_confirmed=True, measured_at=measured, context="fasting")

    def test_bp_confirmation_does_not_require_meal_context(self):
        self.enable_vision()
        self.recognizer.side_effect = lambda *_: copy.deepcopy(BP)
        pid = self.image()["pending_id"]
        self.app.confirm(pid, subject_confirmed=True, measured_at=stamp())
        self.assertEqual(len(self.app.state()["measurements"]), 1)

    def test_missing_device_clock_still_requires_manual_time_to_commit(self):
        self.enable_vision()
        self.recognizer.side_effect = lambda *_: dict(copy.deepcopy(GLUCOSE), device_time_text="")
        result = self.image()
        self.assertIsNone(result["items"][0]["measured_at"])
        with self.assertRaises(InputError):
            self.app.confirm(result["pending_id"], subject_confirmed=True, context="fasting")
        self.assertFalse(self.app.state()["measurements"])
        actual = stamp(datetime.now(TZ) - timedelta(hours=1))
        self.app.confirm(result["pending_id"], subject_confirmed=True, context="fasting", measured_at=actual)
        self.assertEqual(self.app.state()["measurements"][0]["measured_at"], actual)

    def test_wechat_sender_authorization_and_duplicate_before_provider(self):
        self.enable_vision()
        self.live()
        denied = self.image(sender="外人", source="wechat", message_id="image-denied")
        self.assertFalse(denied["accepted"])
        self.preparer.assert_not_called()
        first = self.image(sender="爸爸", source="wechat", message_id="image-1")
        duplicate = self.image(sender="爸爸", source="wechat", message_id="image-1")
        self.assertTrue(first["accepted"])
        self.assertEqual(duplicate["reason"], "duplicate")
        self.assertEqual(self.recognizer.call_count, 1)
        self.assertEqual(len(self.app.state()["pending"]), 1)
        self.assert_no_images_persisted()

    def test_wechat_patient_confirms_own_image_but_proxy_must_identify_dad(self):
        self.enable_vision()
        self.live()
        pid = self.image(sender="家属", source="wechat", message_id="proxy-image")["pending_id"]
        missing = self.app.process_message(f"确认 {pid} 刚测 空腹", "家属", "wechat", "proxy-confirm-1")
        self.assertTrue(missing["needs_correction"])
        confirmed = self.app.process_message(f"确认 {pid} 爸爸 刚测 空腹", "家属", "wechat", "proxy-confirm-2")
        self.assertTrue(confirmed["ok"])
        own_pid = self.image(sender="爸爸", source="wechat", message_id="own-image")["pending_id"]
        own = self.app.process_message(f"确认 {own_pid} 刚测 随机", "爸爸", "wechat", "own-confirm")
        self.assertTrue(own["ok"])
        self.assertEqual(len(self.app.state()["measurements"]), 2)

    def test_route_change_cancels_image_draft(self):
        self.enable_vision()
        self.live()
        pid = self.image(sender="爸爸", source="wechat", message_id="old-image")["pending_id"]
        self.app.update_config({"wechat": {"group_name": "新图片群"}})
        with self.assertRaises(InputError):
            self.app.confirm(pid, subject_confirmed=True, measured_at=stamp(), context="random")
        self.assertFalse(self.app.lease_outbox()["messages"])
        self.assertFalse(self.app.state()["pending"])

    def test_inflight_success_and_failure_cancel_on_route_change(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.enable_vision()
                self.live()
                def delayed(*_):
                    self.app.update_config({"wechat": {"group_name": "新群-" + str(fail)}})
                    if fail:
                        raise VisionError("测试失败")
                    return copy.deepcopy(GLUCOSE)
                self.recognizer.side_effect = delayed
                result = self.image(sender="爸爸", source="wechat", message_id="inflight-" + str(fail))
                self.assertTrue(result["cancelled"])
                self.assertFalse(self.app.state()["pending"])
                self.assertFalse(self.app.lease_outbox()["messages"])

    def test_inflight_stop_disarms_and_cancels(self):
        self.enable_vision()
        self.live()
        def delayed(*_):
            self.app.update_config({"wechat": {"live_enabled": False}})
            return copy.deepcopy(GLUCOSE)
        self.recognizer.side_effect = delayed
        result = self.image(sender="爸爸", source="wechat", message_id="stop-image")
        self.assertTrue(result["cancelled"])
        self.assertFalse(self.app.state()["pending"])

    def test_failure_recoverable_without_blob_or_exception_secret(self):
        self.enable_vision()
        self.live()
        self.recognizer.side_effect = RuntimeError("FAKE-EXCEPTION-SECRET " + RAW_IMAGE)
        failed = self.image(sender="爸爸", source="wechat", message_id="failure")
        self.assertTrue(failed["needs_correction"])
        self.assertEqual(failed["answer_source"], "vision_error")
        self.assertNotIn("FAKE-EXCEPTION-SECRET", json.dumps(failed))
        self.assertFalse(self.app.state()["pending"])
        self.assertFalse(self.app.state()["measurements"])
        self.assertTrue(self.app.config["wechat"]["live_enabled"])
        self.recognizer.side_effect = lambda *_: copy.deepcopy(GLUCOSE)
        recovered = self.image(sender="爸爸", source="wechat", message_id="retry-new-image")
        self.assertIn("pending_id", recovered)
        self.assert_no_images_persisted()

    def test_busy_image_lock_can_recover_without_releasing_other_owner(self):
        self.enable_vision()
        self.app.image_lock.acquire()
        try:
            result = self.image()
            self.assertTrue(result["needs_correction"])
            self.assertTrue(self.app.image_lock.locked())
            self.recognizer.assert_not_called()
        finally:
            self.app.image_lock.release()
        self.assertIn("pending_id", self.image())

    def test_unclear_food_and_report_results_do_not_create_records(self):
        self.enable_vision()
        for category in ("glucose_meter", "food", "report"):
            self.recognizer.side_effect = lambda *_, c=category: {"category": c, "readings": [], "summary": "没有可核对的测量读数", "warnings": []}
            with self.subTest(category=category):
                result = self.image()
                self.assertNotIn("pending_id", result)
                self.assertFalse(result["items"])
                self.assertFalse(self.app.state()["pending"])
                self.assertFalse(self.app.state()["measurements"])
        self.assert_no_images_persisted()

    def test_acute_caption_bypasses_all_image_calls_even_when_disabled(self):
        result = self.image(text="爸爸现在突然嘴歪，说话不清")
        self.assertEqual(result["answer_source"], "rules")
        self.assertEqual(result["assessment"]["level"], "urgent")
        self.assertIn("120", result["reply"])
        self.preparer.assert_not_called()
        self.recognizer.assert_not_called()
        self.assertFalse(self.app.state()["pending"])
        self.assert_no_images_persisted()

    def test_medication_caption_bypasses_model(self):
        self.enable_vision()
        result = self.image(text="这个血糖现在胰岛素打几单位")
        self.assertEqual(result["answer_source"], "rules")
        self.recognizer.assert_not_called()


class ImageHttpTests(ImageFixture, unittest.TestCase):
    def setUp(self):
        self.start_app()
        self.enable_vision()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.app = self.app
        self.url = "http://127.0.0.1:" + str(self.http.server_address[1])
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        self.stop_app()

    def post(self, path, body, headers=None):
        request = Request(self.url + path, data=json.dumps(body).encode(), headers=headers or {"X-CSRF-Token": self.app.csrf})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def assert_http_error(self, status, path, body, headers):
        with self.assertRaises(HTTPError) as caught:
            self.post(path, body, headers)
        self.assertEqual(caught.exception.code, status)
        caught.exception.close()

    def test_image_endpoint_requires_csrf_and_same_origin(self):
        for headers in ({"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": self.app.csrf, "Origin": "https://untrusted.example"}):
            self.assert_http_error(403, "/api/image", {"image_base64": RAW_IMAGE}, headers)
        self.recognizer.assert_not_called()
        result = self.post("/api/image", {"image_base64": RAW_IMAGE})
        self.assertIn("pending_id", result)
        self.assertFalse(self.app.state()["measurements"])

    def test_image_http_confirm_requires_true_boolean_and_actual_details(self):
        pid = self.post("/api/image", {"image_base64": RAW_IMAGE})["pending_id"]
        payload = {"pending_id": pid, "measured_at": stamp(), "context": "fasting", "subject_confirmed": "true"}
        self.assert_http_error(400, "/api/confirm", payload, {"X-CSRF-Token": self.app.csrf})
        payload["subject_confirmed"] = True
        self.assertTrue(self.post("/api/confirm", payload)["ok"])
        self.assertEqual(len(self.app.state()["measurements"]), 1)

    def test_bridge_image_token_group_sender_and_duplicate_guards(self):
        self.live()
        payload = {"group_name": "图片测试群", "sender": "爸爸", "message_id": "http-photo", "image_base64": RAW_IMAGE}
        self.assert_http_error(403, "/api/bridge/image", payload, {"Authorization": "Bearer WRONG"})
        auth = {"Authorization": "Bearer " + self.app.config["bridge_token"]}
        self.assert_http_error(403, "/api/bridge/image", dict(payload, group_name="未授权群"), auth)
        denied = self.post("/api/bridge/image", dict(payload, sender="外人"), auth)
        self.assertFalse(denied["accepted"])
        self.recognizer.assert_not_called()
        result = self.post("/api/bridge/image", payload, auth)
        self.assertIn("pending_id", result)
        self.assertEqual(self.post("/api/bridge/image", payload, auth)["reason"], "duplicate")
        self.assertEqual(self.recognizer.call_count, 1)

    def test_image_error_endpoint_recoverable_sanitized_and_deduplicated(self):
        self.live()
        auth = {"Authorization": "Bearer " + self.app.config["bridge_token"]}
        payload = {"group_name": "图片测试群", "sender": "爸爸", "message_id": "download-error", "error": "PRIVATE-PATH-OR-SECRET"}
        result = self.post("/api/bridge/image-error", payload, auth)
        self.assertTrue(result["needs_correction"])
        self.assertNotIn("PRIVATE-PATH-OR-SECRET", json.dumps(result))
        self.assertEqual(self.post("/api/bridge/image-error", payload, auth)["reason"], "duplicate")
        self.recognizer.assert_not_called()
        self.assertFalse(self.app.state()["measurements"])

    def test_image_endpoint_rejects_oversized_body_before_reading_or_provider(self):
        headers = {"X-CSRF-Token": self.app.csrf, "Content-Length": str(12 * 1024 * 1024 + 1)}
        self.assert_http_error(400, "/api/image", {}, headers)
        self.recognizer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
