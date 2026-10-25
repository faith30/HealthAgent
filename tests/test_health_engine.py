import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from health_engine import parse_measurements, assess, offline_answer

NOW = "2026-10-25T08:00:00+08:00"


class ParsingTests(unittest.TestCase):
    def test_combined_explicit_readings(self):
        result = parse_measurements("血压138/82，空腹血糖7.2 mmol/L", NOW)
        self.assertEqual([x["kind"] for x in result["items"]], ["bp", "glucose"])
        self.assertEqual(result["items"][1]["context"], "fasting")
        self.assertEqual(result["items"][0]["measured_at"], NOW)

    def test_missing_unit_is_unconfirmed(self):
        result = parse_measurements("空腹血糖3.0", NOW)
        self.assertFalse(result["items"][0]["unit_confirmed"])
        self.assertTrue(result["warnings"])
        self.assertEqual(assess(result["items"], "", {})["level"], "routine")

    def test_mg_conversion(self):
        result = parse_measurements("餐后2小时血糖180 mg/dL", NOW)
        self.assertEqual(result["items"][0]["value"], 10.0)
        self.assertEqual(result["items"][0]["original_value"], 180)
        self.assertEqual(result["items"][0]["context"], "postmeal2h")

    def test_unknown_context_does_not_become_fasting(self):
        item = parse_measurements("血糖8 mmol/L", NOW)["items"][0]
        self.assertEqual(item["context"], "unknown")
        self.assertEqual(assess([item], "", {"doctor_targets": {"glucose_fasting_high": 7}})["level"], "routine")

    def test_nonfasting_is_unknown(self):
        item = parse_measurements("不是空腹血糖8 mmol/L", NOW)["items"][0]
        self.assertEqual(item["context"], "unknown")

    def test_plain_postmeal_not_two_hours(self):
        self.assertEqual(parse_measurements("餐后血糖8 mmol/L", NOW)["items"][0]["context"], "unknown")

    def test_invalid_bp_rejected(self):
        for text in ["血压80/120", "血压500/80", "血压-1/70"]:
            result = parse_measurements(text, NOW)
            self.assertEqual(result["items"], [])
            self.assertTrue(result["warnings"])

    def test_historical_time_not_silently_now(self):
        result = parse_measurements("昨天血压130/80", NOW)
        self.assertIsNone(result["items"][0]["measured_at"])
        self.assertTrue(result["warnings"])

    def test_unsupported_units_rejected(self):
        self.assertEqual(parse_measurements("血糖7 mol/L", NOW)["items"], [])

    def test_extreme_reading_not_ignored(self):
        result = parse_measurements("血糖999 mmol/L", NOW)
        self.assertEqual(result["items"], [])
        self.assertIn("医疗", "".join(result["warnings"]))


class SafetyTests(unittest.TestCase):
    def reading(self, text):
        return parse_measurements(text, NOW)["items"]

    def test_current_stroke_even_after_resolving(self):
        for text in ["爸爸突然嘴歪", "刚才说话不清，现在缓解了", "现在右手抬不起来", "爸爸现在一侧肢体无力"]:
            self.assertEqual(assess([], text)["level"], "urgent", text)

    def test_negated_historical_hypothetical_not_current_alert(self):
        for text in ["爸爸没有嘴歪", "三个月前说话不清", "如果嘴歪该怎么办", "说话不清是什么意思", "没有意识不清", "没有任何说话不清", "没有嘴歪和说话不清", "脑梗后一直说话不清"]:
            self.assertNotEqual(assess([], text)["level"], "urgent", text)

    def test_history_does_not_mask_new_episode(self):
        self.assertEqual(assess([], "以前有嘴歪，现在又突然嘴歪了")["level"], "urgent")

    def test_hypoglycemia_and_unconscious(self):
        self.assertEqual(assess(self.reading("血糖3.2 mmol/L"), "")["level"], "attention")
        result = assess(self.reading("血糖3.2 mmol/L"), "爸爸叫不醒")
        self.assertEqual(result["level"], "urgent")
        self.assertIn("不要强行", "".join(result["messages"]))

    def test_no_default_individual_target(self):
        self.assertEqual(assess(self.reading("血压155/95，空腹血糖9 mmol/L"), "")["level"], "routine")
        configured = {"doctor_targets": {"bp_systolic_high": 140, "glucose_fasting_high": 7}}
        self.assertEqual(assess(self.reading("血压155/95，空腹血糖9 mmol/L"), "", configured)["level"], "attention")

    def test_very_high_bp_with_and_without_symptoms(self):
        items = self.reading("血压190/110")
        self.assertEqual(assess(items, "没有胸痛")["level"], "attention")
        self.assertEqual(assess(items, "现在胸痛")["level"], "urgent")

    def test_low_bp_only_clinician_target(self):
        items = self.reading("血压95/65")
        self.assertEqual(assess(items, "")["level"], "routine")
        self.assertEqual(assess(items, "", {"doctor_targets": {"bp_systolic_low": 100}})["level"], "attention")

    def test_offline_medicine_and_menu_boundaries(self):
        self.assertIn("不能", offline_answer("胰岛素打多少"))
        self.assertIn("资料未齐", offline_answer("今天吃什么"))
        self.assertIn("出院医生菜单", offline_answer("今天吃什么", {"doctor_plan": "出院医生菜单"}))


if __name__ == "__main__":
    unittest.main()
