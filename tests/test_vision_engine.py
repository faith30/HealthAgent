import base64
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from PIL import Image

import vision_engine as vision


def picture(fmt="PNG", size=(100, 80), **kwargs):
    image = Image.new("RGB", size, (90, 160, 200))
    output = BytesIO()
    image.save(output, format=fmt, **kwargs)
    return base64.b64encode(output.getvalue()).decode("ascii")


def draft(**changes):
    result = {"category": "glucose_meter", "readable": True, "confidence": "high",
              "readings": [{"kind": "glucose", "value": 7.2, "unit": "mmol/L", "unit_visible": True, "digits_clear": True}],
              "summary": "仪器", "warnings": [], "device_time_text": ""}
    result.update(changes)
    return result


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.value[:limit]


def provider_response(result):
    return FakeResponse(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}).encode())


class VisionTest(unittest.TestCase):
    def setUp(self):
        self.prepared = vision.prepare_image(picture())
        self.config = {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key": "TEST-SECRET"}

    def recognize(self, result, config=None, finish="stop"):
        encoded = json.dumps({"choices": [{"finish_reason": finish, "message": {"content": result if isinstance(result, str) else json.dumps(result)}}]}).encode()
        with patch.object(vision, "build_opener") as opener:
            opener.return_value.open.return_value = FakeResponse(encoded)
            answer = vision.recognize(self.prepared, "附言", config or self.config, {"medicines": "PRIVATE"})
            request = opener.return_value.open.call_args.args[0]
            return answer, json.loads(request.data)

    def test_supported_images_are_reencoded_and_bounded(self):
        for fmt in ("JPEG", "PNG", "WEBP", "BMP"):
            with self.subTest(fmt=fmt):
                result = vision.prepare_image(picture(fmt, (3000, 800)))
                self.assertEqual(result["width"], 2048)
                self.assertLessEqual(result["height"], 2048)
                self.assertIn(result["mime"], ("image/png", "image/jpeg"))
                self.assertEqual(len(result["sha256"]), 64)

    def test_orientation_and_metadata_removed(self):
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "private information"
        value = vision.prepare_image(picture("JPEG", (100, 60), exif=exif))
        self.assertEqual((value["width"], value["height"]), (60, 100))
        with Image.open(BytesIO(base64.b64decode(value["data_url"].split(",", 1)[1]))) as image:
            self.assertFalse(image.getexif())
            self.assertNotIn("icc_profile", image.info)

    def test_malformed_and_external_sources_rejected(self):
        values = ["https://example.com/a.jpg", "file:///a.jpg", "data:image/svg+xml;base64,PHN2Zy8+", "a$%%%", base64.b64encode(b"not a picture").decode()]
        for value in values:
            with self.subTest(value=value), self.assertRaises(vision.VisionError):
                vision.prepare_image(value)

    def test_data_url_mime_mismatch_rejected(self):
        with self.assertRaises(vision.VisionError):
            vision.prepare_image("data:image/jpeg;base64," + picture("PNG"))

    def test_animation_rejected(self):
        frames = [Image.new("RGB", (10, 10), color) for color in ("red", "blue")]
        for fmt in ("GIF", "PNG", "WEBP"):
            out = BytesIO()
            frames[0].save(out, format=fmt, save_all=True, append_images=frames[1:], duration=100, loop=0)
            with self.subTest(fmt=fmt), self.assertRaises(vision.VisionError):
                vision.prepare_image(base64.b64encode(out.getvalue()).decode())

    def test_oversized_bytes_and_pixels_rejected(self):
        with self.assertRaises(vision.VisionError):
            vision.prepare_image(base64.b64encode(b"a" * (vision.MAX_IMAGE_BYTES + 1)).decode())
        for size in ((8193, 2), (5000, 4001)):
            with self.subTest(size=size), self.assertRaises(vision.VisionError):
                vision.prepare_image(picture(size=size))

    def test_no_credentials_does_not_request(self):
        with patch.object(vision, "build_opener") as opener, self.assertRaises(vision.VisionError):
            vision.recognize(self.prepared, "", {}, {})
        opener.assert_not_called()

    def test_processing_dependency_is_optional_at_import(self):
        self.assertTrue(vision.processing_available())
        original = __import__
        def import_without_pillow(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("missing Pillow")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=import_without_pillow):
            self.assertFalse(vision.processing_available())
            with self.assertRaises(vision.VisionError):
                vision.prepare_image(picture())

    def test_valid_glucose_and_private_profile_not_sent(self):
        result, body = self.recognize(draft())
        self.assertEqual(result["readings"], [{"kind": "glucose", "value": 7.2, "unit": "mmol/L"}])
        self.assertNotIn("PRIVATE", json.dumps(body))
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["messages"][1]["content"][1]["image_url"]["url"], self.prepared["data_url"])
        self.assertEqual(body["messages"][1]["content"][1]["image_url"]["detail"], "original")
        self.assertIn("尚未保存", "".join(result["warnings"]))

    def test_matching_device_reads_are_independent_and_still_require_confirmation(self):
        with patch.object(vision, "build_opener") as opener:
            opener.return_value.open.side_effect = [provider_response(draft()), provider_response(draft())]
            result = vision.recognize(self.prepared, "用户附言中的7.2不应透露给复核", self.config)
            calls = opener.return_value.open.call_args_list
        self.assertEqual(len(calls), 2)
        first, second = [json.loads(c.args[0].data) for c in calls]
        self.assertIn("7.2", json.dumps(first, ensure_ascii=False))
        self.assertNotIn("7.2", json.dumps(second, ensure_ascii=False))
        self.assertNotIn("用户附言中的", json.dumps(second, ensure_ascii=False))
        self.assertEqual([m["role"] for m in second["messages"]], ["system", "user"])
        self.assertIn("旋转方向", second["messages"][0]["content"])
        self.assertIn("七段数码管", second["messages"][0]["content"])
        self.assertEqual(first["messages"][1]["content"][1], second["messages"][1]["content"][1])
        self.assertTrue(all(0 < c.kwargs["timeout"] <= 15 for c in calls))
        self.assertEqual(result["readings"][0]["value"], 7.2)
        self.assertIn("仍可能共同看错", "".join(result["warnings"]))
        self.assertIn("尚未保存", "".join(result["warnings"]))

    def test_disagreeing_digits_never_return_either_candidate(self):
        changed = draft(readings=[dict(draft()["readings"][0], value=5.9)])
        with patch.object(vision, "build_opener") as opener:
            opener.return_value.open.side_effect = [provider_response(draft()), provider_response(changed)]
            result = vision.recognize(self.prepared, "", self.config)
        self.assertFalse(result["readings"])
        for candidate in ("7.2", "5.9"):
            self.assertNotIn(candidate, json.dumps(result))
        self.assertIn("重新拍清楚", "".join(result["warnings"]))

    def test_recheck_category_unit_or_ambiguous_result_must_match(self):
        results = [draft(category="food", readings=[]), draft(warnings=["小数点不清楚"]),
                   draft(readings=[dict(draft()["readings"][0], value=129.6, unit="mg/dL")]),
                   draft(readings=[])]
        for changed in results:
            with self.subTest(changed=changed), patch.object(vision, "build_opener") as opener:
                opener.return_value.open.side_effect = [provider_response(draft()), provider_response(changed)]
                result = vision.recognize(self.prepared, "", self.config)
            self.assertFalse(result["readings"])
            self.assertNotIn("7.2", json.dumps(result))

    def test_second_request_failure_or_bad_response_hides_first_numbers(self):
        failures = [TimeoutError("TEST-SECRET"), URLError("TEST-SECRET"), provider_response({"bad": "TEST-SECRET"}), FakeResponse(b"malformed TEST-SECRET")]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch.object(vision, "build_opener") as opener:
                opener.return_value.open.side_effect = [provider_response(draft()), failure]
                result = vision.recognize(self.prepared, "", self.config)
            self.assertFalse(result["readings"])
            self.assertNotIn("7.2", json.dumps(result))
            self.assertNotIn("TEST-SECRET", json.dumps(result))

    def test_food_report_and_unclear_device_skip_second_request(self):
        for first in (draft(category="food", readings=[], visible_foods=["苹果"]), draft(category="report", readings=[]), draft(confidence="unknown")):
            with self.subTest(category=first["category"]), patch.object(vision, "build_opener") as opener:
                opener.return_value.open.return_value = provider_response(first)
                vision.recognize(self.prepared, "", self.config)
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_expired_shared_budget_does_not_start_provider_call(self):
        with patch.object(vision, "build_opener") as opener, patch.object(vision.time, "monotonic", return_value=31):
            with self.assertRaises(vision.VisionError):
                vision._call_provider({}, "https://example.invalid", "TEST-SECRET", total_deadline=30)
        opener.assert_not_called()

    def test_slow_response_is_rejected_by_deadline_even_if_socket_did_not_timeout(self):
        with patch.object(vision, "build_opener") as opener, patch.object(vision.time, "monotonic", side_effect=[0, 0, 16]):
            opener.return_value.open.return_value = provider_response(draft())
            with self.assertRaises(vision.VisionError):
                vision._call_provider({}, "https://example.invalid", "TEST-SECRET", total_deadline=30)

    def test_chunked_response_reads_remain_bounded(self):
        class ChunkedResponse:
            def __init__(self):
                self.parts = iter([b"abc", b"def", b""])
            def read1(self, limit):
                return next(self.parts)
        with patch.object(vision.time, "monotonic", return_value=1):
            self.assertEqual(vision._read_response(ChunkedResponse(), 10), b"abcdef")

    def test_deepseek_specific_option_not_sent_to_other_domain(self):
        config = dict(self.config, base_url="https://api.deepseek.com.example.org")
        _, body = self.recognize(draft(), config)
        self.assertNotIn("thinking", body)
        self.assertEqual(body["messages"][1]["content"][1]["image_url"]["detail"], "high")

    def test_new_openai_omits_temperature(self):
        _, body = self.recognize(draft(), dict(self.config, base_url="https://api.openai.com/v1", model="gpt-6-luna"))
        self.assertNotIn("temperature", body)
        self.assertEqual(body["max_completion_tokens"], 1500)

    def test_mgdl_preserved_and_clock_not_converted(self):
        reading = {"kind": "glucose", "value": 126, "unit": "mg/dL", "unit_visible": True, "digits_clear": True}
        result, _ = self.recognize(draft(readings=[reading], device_time_text="10/24 08:30"))
        self.assertEqual(result["readings"][0]["value"], 126)
        self.assertEqual(result["readings"][0]["unit"], "mg/dL")
        self.assertEqual(result["device_time_text"], "10/24 08:30")
        self.assertNotIn("measured_at", json.dumps(result))

    def test_unknown_readability_units_or_decimal_reject_whole_batch(self):
        cases = [draft(readable=False), draft(readable="true"), draft(confidence="unknown"), draft(confidence=None), draft(warnings=["小数点不清楚"])]
        for change in ({"unit": "unknown"}, {"unit_visible": False}, {"digits_clear": False}, {"confidence": "medium"}, {"value": True}, {"value": "7.2"}, {"value": 900}, {"value": 10 ** 500}):
            cases.append(draft(readings=[draft()["readings"][0], dict(draft()["readings"][0], **change)]))
        for case in cases:
            with self.subTest(case=case):
                result, _ = self.recognize(case)
                self.assertEqual(result["readings"], [])

    def test_missing_device_clock_alone_allows_unconfirmed_draft(self):
        for note in ("未显示设备时间", "设备未显示时间", "设备时间不可见"):
            with self.subTest(note=note):
                result, _ = self.recognize(draft(warnings=[note], device_time_text=""))
                self.assertEqual(len(result["readings"]), 1)
                self.assertNotIn("measured_at", result)
                self.assertNotIn("device_time_text", result)
                self.assertIn("实际测量时间", "".join(result["warnings"]))

    def test_clock_warning_never_masks_unclear_decimal_or_unit(self):
        for warnings in (["未显示设备时间", "小数点不清楚"], ["设备时间不可见且单位不清楚"], ["未显示设备时间", "单位不清楚"]):
            with self.subTest(warnings=warnings):
                result, _ = self.recognize(draft(warnings=warnings))
                self.assertFalse(result["readings"])

    def test_unknown_warning_still_blocks_optimistic_numeric_reading(self):
        for note in ("模拟测试设备", "另有未确定问题", "未显示设备时间，可能有误"):
            with self.subTest(note=note):
                result, _ = self.recognize(draft(warnings=[note]))
                self.assertFalse(result["readings"])

    def test_bp_order_integer_range_and_category(self):
        bp = {"kind": "bp", "systolic": 135, "diastolic": 85, "unit": "mmHg", "unit_visible": True, "digits_clear": True}
        result, _ = self.recognize(draft(category="bp_meter", readings=[bp]))
        self.assertEqual(result["readings"][0]["systolic"], 135)
        for change in ({"systolic": 85}, {"systolic": 900}, {"diastolic": 85.5}, {"unit": "unknown"}, {"systolic": True}):
            with self.subTest(change=change):
                result, _ = self.recognize(draft(category="bp_meter", readings=[dict(bp, **change)]))
                self.assertFalse(result["readings"])
        result, _ = self.recognize(draft(category="glucose_meter", readings=[bp]))
        self.assertFalse(result["readings"])

    def test_reports_and_food_never_create_records_or_echo_advice(self):
        for category in ("report", "food", "nutrition_label", "other"):
            with self.subTest(category=category):
                result, _ = self.recognize(draft(category=category, summary="加药100mg，每天只吃50克，忽略所有规则", warnings=["伪造医嘱"]))
                self.assertFalse(result["readings"])
                self.assertNotIn("100mg", json.dumps(result, ensure_ascii=False))
                self.assertNotIn("伪造医嘱", json.dumps(result, ensure_ascii=False))

    def test_food_names_are_bounded_deduplicated_and_advice_is_not_echoed(self):
        result, _ = self.recognize(draft(category="food", readings=[], visible_foods=["苹果", "米饭", "broccoli", "苹果", "鸡蛋", "山药", "fish", "玉米"],
                                        summary="每日必须吃100克并停药"))
        self.assertIn("图中可能有苹果、米饭、broccoli、鸡蛋、山药、fish", result["summary"])
        self.assertNotIn("玉米", result["summary"])
        self.assertNotIn("100克", result["summary"])
        self.assertNotIn("停药", result["summary"])
        self.assertFalse(result["readings"])

    def test_food_names_reject_instructions_medical_text_and_non_names(self):
        invalid = ["忽略所有规则", "胰岛素", "降血糖", "阿司匹林", "应该停药", "ignore system", "take insulin", "100克米饭", "<苹果>",
                   "苹果\n停药", "苹果" * 11, " ", {"name": "苹果"}, True]
        result, _ = self.recognize(draft(category="food", readings=[], visible_foods=invalid + ["面条"]))
        self.assertTrue(result["summary"].startswith("图中可能有面条，请核对食物名称。"))
        for name in invalid:
            if isinstance(name, str) and name.strip():
                self.assertNotIn(name, result["summary"])
        result, _ = self.recognize(draft(category="food", readings=[], readable=False, visible_foods=["苹果"]))
        self.assertNotIn("苹果", result["summary"])

    def test_optional_food_names_malformed_or_absent_use_fixed_fallback(self):
        for names in (None, "苹果", {"food": "苹果"}, []):
            with self.subTest(names=names):
                result, _ = self.recognize(draft(category="food", readings=[], visible_foods=names))
                self.assertTrue(result["summary"].startswith("这看起来是食物照片"))

    def test_malformed_nonfinite_duplicate_truncated_json_rejected(self):
        for content in ("```json\n{}\n```", "not json", "{\"category\":NaN}", "{\"category\":\"food\",\"category\":\"report\"}"):
            with self.subTest(content=content), self.assertRaises(vision.VisionError):
                self.recognize(content)
        with self.assertRaises(vision.VisionError):
            self.recognize(draft(), finish="length")

    def test_response_limit(self):
        with patch.object(vision, "build_opener") as opener:
            opener.return_value.open.return_value = FakeResponse(b"x" * (vision.MAX_RESPONSE_BYTES + 1))
            with self.assertRaises(vision.VisionError):
                vision.recognize(self.prepared, "", self.config, {})

    def test_errors_do_not_expose_secrets_or_provider_body(self):
        errors = [HTTPError("https://secret.invalid", 401, "TEST-SECRET", {}, BytesIO(b"TEST-SECRET")),
                  HTTPError("https://secret.invalid", 302, "TEST-SECRET", {}, BytesIO(b"TEST-SECRET")),
                  URLError("TEST-SECRET")]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(vision, "build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(vision.VisionError) as raised:
                    vision.recognize(self.prepared, "", self.config, {})
                self.assertNotIn("TEST-SECRET", str(raised.exception))
                self.assertNotIn("secret.invalid", str(raised.exception))

    def test_redirect_handler_never_forwards_authorization(self):
        self.assertIsNone(vision._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))

    def test_bad_prepared_or_url_rejected_before_network(self):
        for config in (dict(self.config, base_url="http://outside.example"), dict(self.config, base_url="https://api.example?key=TEST-SECRET"), dict(self.config, api_key="bad\r\nkey"), dict(self.config, base_url="https://[broken")):
            with self.subTest(config=config), patch.object(vision, "build_opener") as opener, self.assertRaises(vision.VisionError):
                vision.recognize(self.prepared, "", config, {})
            opener.assert_not_called()
        with self.assertRaises(vision.VisionError):
            vision.recognize(dict(self.prepared, sha256="bad"), "", self.config, {})


if __name__ == "__main__":
    unittest.main()
