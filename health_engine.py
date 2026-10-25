"""Conservative local health-record parsing and educational replies.

This module does not diagnose disease or select treatment. Parsed records are
proposals: the caller must display them and obtain confirmation before saving.
Clinical rules use explicitly supplied units and clinician-set targets only,
apart from published urgent symptom / hypoglycaemia / very-high-BP rules.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import math
import re
import unicodedata

SHANGHAI = timezone(timedelta(hours=8))
SOURCES = [
    {"id": "food", "title": "国家卫健委：成人糖尿病食养指南问答", "url": "https://www.nhc.gov.cn/wjw/c100378/202301/7faed9993c9b45aea158c5335346f282/files/1732844468167_10535.pdf"},
    {"id": "glucose", "title": "CDC：血糖监测与个体目标", "url": "https://www.cdc.gov/diabetes/diabetes-testing/monitoring-blood-sugar.html"},
    {"id": "bp", "title": "AHA：家庭血压测量", "url": "https://www.heart.org/en/health-topics/high-blood-pressure/understanding-blood-pressure-readings/monitoring-your-blood-pressure-at-home"},
    {"id": "high_bp", "title": "AHA：严重高血压的处理", "url": "https://www.heart.org/en/health-topics/high-blood-pressure/understanding-blood-pressure-readings/when-to-call-911-for-high-blood-pressure"},
    {"id": "stroke", "title": "美国卒中协会：卒中警示症状", "url": "https://www.stroke.org/en/about-stroke/stroke-symptoms"},
    {"id": "swallow", "title": "美国卒中协会：卒中后的吞咽障碍", "url": "https://www.stroke.org/en/about-stroke/effects-of-stroke/physical-effects/dysphagia/what-you-should-know-about-dysphagia"},
    {"id": "hypo", "title": "ADA：低血糖", "url": "https://diabetes.org/living-with-diabetes/hypoglycemia-low-blood-glucose"},
    {"id": "high_glucose", "title": "CDC：糖尿病酮症酸中毒警示", "url": "https://www.cdc.gov/diabetes/about/diabetic-ketoacidosis.html"},
]

TIPS = [
    "主食要定量，搭配蔬菜和适量蛋白质食物；不要为了控糖把主食全部停掉。",
    "土豆、红薯、玉米也提供较多淀粉，安排这一餐时要计入主食总量。",
    "无糖标签不代表可以不限量吃，饼干、糕点里的淀粉和油脂也需要留意。",
    "做菜逐步少放盐，酱油、咸菜和加工肉里的盐也要计算在内。",
    "水果不需要一概禁食，份量、整餐搭配和自己的血糖反应都需要考虑。",
    "先安静坐5分钟再量血压，上臂放松并与心脏同高；两次测量间隔1分钟，保留两次读数。",
    "把血糖测量时间、空腹或餐后情况一起记下来，复诊时比只有一个数字更有帮助。",
]


def _clean(text):
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def _measurement_time(text, now_iso):
    current = now_iso or datetime.now(SHANGHAI).isoformat(timespec="seconds")
    # Do not invent historical timestamps or merge clock times with today's date.
    if re.search(r"昨天|前天|上周|上个月|去年|\d+天前|\d+小时前|\d+分钟前|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}[日号]|\d{1,2}:\d{2}|\d{1,2}点|早上|上午|下午|晚上|昨晚", text):
        return None, "消息包含具体或历史测量时间，请在确认记录时填写实际测量时间；没有按当前时间代填。"
    return current, None


def _context(text, start, end):
    left = max(text.rfind(mark, 0, start) for mark in ["，", ",", "；", ";", "。", "\n"])
    rights = [text.find(mark, end) for mark in ["，", ",", "；", ";", "。", "\n"]]
    right = min((p for p in rights if p >= 0), default=len(text))
    # A clause can contain more than one reading. Match the nearest preceding
    # meal marker, not an earlier reading's context.
    before = text[left + 1:start]
    markers = list(re.finditer(r"(?:不是|非|不算|没有|不)?\s*空腹|(?:餐后|饭后)\s*(?:2|两|二)\s*(?:小时|h)|餐前|饭前|随机", before, re.I))
    if markers:
        segment = markers[-1].group(0)
    else:
        segment = text[left + 1:right]
    if re.search(r"(?:不是|非|不算|没有|不)\s*空腹", segment):
        return "unknown"
    if "空腹" in segment:
        return "fasting"
    if re.search(r"餐后\s*(?:2|两|二)\s*(?:小时|h)|饭后\s*(?:2|两|二)\s*(?:小时|h)", segment, re.I):
        return "postmeal2h"
    if "餐前" in segment or "饭前" in segment:
        return "premeal"
    if "随机" in segment:
        return "random"
    return "unknown"


def parse_measurements(text, now_iso=None):
    """Return draft readings and warnings; no persistence or diagnoses.

    Extra flags are intentionally explicit: ``requires_confirmation`` is always
    true, and an omitted glucose unit yields ``unit_confirmed=False``. Values in
    mg/dL are converted with divisor 18 and retain their original value/unit.
    Out-of-range values are rejected as recording errors, not deemed harmless.
    """
    text = _clean(text)
    items, warnings = [], []
    measured_at, time_warning = _measurement_time(text, now_iso)
    bp_pattern = re.compile(r"(?:血压|\bBP\b)\s*[:：是为=]?\s*(-?\d+(?:\.\d+)?)\s*[/／\\]\s*(-?\d+(?:\.\d+)?)", re.I)
    matches = list(bp_pattern.finditer(text))
    if not matches and re.fullmatch(r"\d{2,3}\s*[/／]\s*\d{2,3}(?:\s*mmHg)?", text, re.I):
        matches = list(re.finditer(r"(\d+)\s*[/／]\s*(\d+)", text))
    for match in matches:
        systolic, diastolic = float(match.group(1)), float(match.group(2))
        if not (20 <= systolic <= 350 and 10 <= diastolic <= 250 and systolic > diastolic):
            warnings.append("血压数值或高低压顺序不合理，请核对血压计并重新输入；如果人不舒服请及时联系医生。")
            continue
        items.append({"kind": "bp", "systolic": systolic, "diastolic": diastolic,
                      "unit": "mmHg", "context": "unknown", "measured_at": measured_at,
                      "requires_confirmation": True, "unit_confirmed": True,
                      "raw": match.group(0)})

    glucose_pattern = re.compile(r"(?:血糖|\bGLU\b)\s*[:：是为=]?\s*(-?\d+(?:\.\d+)?)\s*(mmol\s*/\s*l|毫摩尔\s*/\s*升|mg\s*/\s*dl|毫克\s*/\s*分升)?", re.I)
    for match in glucose_pattern.finditer(text):
        original = float(match.group(1))
        unit_text = re.sub(r"\s", "", match.group(2) or "").lower()
        unit_confirmed = bool(unit_text)
        unit_is_mg = unit_text in ("mg/dl", "毫克/分升")
        value = original / 18.0 if unit_is_mg else original
        # Reject unit-like suffixes that the parser does not understand.
        remainder = text[match.end():match.end() + 12]
        if not unit_confirmed and re.match(r"\s*(?:mol|mg|mmol|g\s*/|毫克|毫摩尔)", remainder, re.I):
            warnings.append("血糖单位未识别，请明确使用 mmol/L 或 mg/dL。")
            continue
        if not (0.5 <= value <= 60):
            warnings.append("血糖数值超出本工具可解析范围，请核对数字和单位后重新输入；数值极端或人不舒服时立即联系医疗人员。")
            if not unit_confirmed:
                warnings.append("尚未提供血糖单位；例如 mg/dL 的数值不能直接当作 mmol/L。")
            continue
        context = _context(text, match.start(), match.end())
        if not unit_confirmed:
            warnings.append("血糖未写单位，草稿暂显示 mmol/L；请核对仪器单位后确认，暂不按这个数值判断高低。")
        if context == "unknown":
            warnings.append("血糖测量餐次尚未明确，请选择空腹、餐前、餐后2小时或随机；不会套用空腹目标。")
        item = {"kind": "glucose", "value": round(value, 2), "unit": "mmol/L",
                "context": context, "measured_at": measured_at,
                "requires_confirmation": True, "unit_confirmed": unit_confirmed,
                "raw": match.group(0)}
        if unit_is_mg:
            item.update(original_value=original, original_unit="mg/dL")
        items.append(item)
    if items and time_warning:
        warnings.append(time_warning)
    if not items and re.search(r"血糖\s*(?:HI|LO)\b", text, re.I):
        warnings.append("仪器显示 HI/LO 时不能当作普通数值记录。请按仪器说明复核，并立即联系医疗人员；意识异常或明显不适时拨打120。")
    return {"items": items, "warnings": list(dict.fromkeys(warnings))}


_STROKE = r"嘴歪|口角歪|说话不清|言语不清|说不出话|一侧(?:手|胳膊|肢体|身体|腿)?(?:突然)?(?:无力|麻木)|单侧(?:肢体)?(?:无力|麻木)|(?:左|右)(?:手|腿|胳膊)(?:突然)?(?:无力|抬不起来)|突然(?:看不清|看不见|失去平衡|走不稳)"
_SEVERE = r"昏迷|意识不清|意识模糊|叫不醒|失去意识|抽搐|无法吞咽|不能吞咽"
_CARDIO = r"胸痛|胸口痛|呼吸困难|喘不过气"


def _active_symptom(text, pattern):
    """Conservative string triage, not a natural-language medical classifier.

    Old/hypothetical/negated statements do not trigger. Explicit current onset
    in a later clause overrides old history. Recent symptoms that have resolved
    still trigger because possible TIA requires urgent evaluation.
    """
    for clause in re.split(r"[，,。；;!?！？\n]", _clean(text)):
        for match in re.finditer(pattern, clause):
            prefix = clause[:match.start()]
            nearby = prefix[-12:]
            suffix = clause[match.end():]
            if re.search(r"如果|假如|万一|假设|是否会|会不会|什么叫|预防|如何防|担心会", prefix):
                continue
            if re.search(r"(?:没有|并无|无|否认|未出现|未见|不是)(?:任何|明显|新的|新发|出现|发生|了)*\s*$", nearby):
                continue
            if re.search(r"(?:并没有|没有|无)(?:嘴歪|口角歪|说话不清|言语不清|一侧无力|昏迷)(?:或|和|及|、)$", nearby):
                continue
            recent = bool(re.search(r"现在|刚刚|刚才|突然|今天|目前|新出现|刚出现", prefix))
            if re.search(r"以前|曾经|病史|出院时|三个月前|\d+个月前|去年|过去|长期|一直", prefix) and not recent:
                continue
            if re.search(r"(?:是什么(?:意思)?|是啥|的意思|有哪些|会怎样|怎么办)[？?]?$", suffix) and not recent and not re.search(r"我|爸爸|父亲|他", prefix):
                continue
            return True
    return False


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def assess(items, text, config=None):
    """Return alert advice; never adjust medicines or label readings normal."""
    config = config or {}
    targets = config.get("doctor_targets", {}) or {}
    level = "routine"
    messages = []
    if _active_symptom(text, _STROKE):
        return {"level": "urgent", "messages": ["消息中有新出现或当前卒中警示症状。请立即拨打120并联系身边家属，记下最早出现症状的时间；不要等待机器人分析或自行开车，即使症状已缓解也需要紧急评估。"]}
    severe = _active_symptom(text, _SEVERE)
    if severe:
        return {"level": "urgent", "messages": ["出现意识异常、抽搐或无法安全吞咽等危险表现时，请立即拨打120并让身边家属接手；不要强行喂食、喂水或喂药。"]}
    if _active_symptom(text, _CARDIO):
        return {"level": "urgent", "messages": ["当前出现胸痛、呼吸困难等危险表现，请立即拨打120并联系身边家属；不要等待机器人判断或自行开车。"]}
    for item in items or []:
        if item.get("kind") == "glucose":
            if not item.get("unit_confirmed", False) and not item.get("confirmed", False):
                messages.append("血糖单位尚未确认，暂不判断数值高低；请核对仪器。若有急性不适，不要等待补录。")
                continue
            value = _finite_number(item.get("value"))
            if value is None or item.get("unit") != "mmol/L":
                messages.append("这条血糖记录的单位或数值需要核对。")
                continue
            if value < 3.9:
                level = "attention"
                messages.append("报告的血糖低于3.9 mmol/L，需要立即处理并联系家属。若清醒且能安全吞咽，可补充约15克快速吸收的糖（例如按标签含糖量取葡萄糖片），15分钟后复测，仍低于3.9则再次处理；已有医生专门方案时按其执行。若意识异常或不能安全吞咽，立即拨打120，不要强行喂食喂水。若不能改善或症状加重，立即求助医疗人员。")
            if value >= 16.7:
                level = "attention"
                messages.append("报告的血糖明显偏高，请立即核对数值和单位并联系医疗人员。若复测仍持续在16.7 mmol/L（约300 mg/dL）或以上，或伴呕吐无法进食饮水、呼吸异常、意识异常，请立即去急诊或拨打120；不要自行追加药量。")
            key = {"fasting": "glucose_fasting_high", "postmeal2h": "glucose_postmeal_high"}.get(item.get("context"))
            threshold = _finite_number(targets.get(key)) if key else None
            if threshold is not None and value > threshold:
                level = "attention"
                messages.append("这次血糖高于已录入的医生目标，请核对测量餐次和数值，按医嘱复测并联系家属/医疗团队；不要自行加药。")
        elif item.get("kind") == "bp":
            systolic, diastolic = _finite_number(item.get("systolic")), _finite_number(item.get("diastolic"))
            if systolic is None or diastolic is None or item.get("unit") != "mmHg":
                messages.append("这条血压记录的单位或数值需要核对。")
                continue
            if systolic > 180 or diastolic > 120:
                if _active_symptom(text, _CARDIO):
                    return {"level": "urgent", "messages": ["血压读数很高并伴胸痛或呼吸困难等症状，请立即拨打120、联系身边家属，不要等待血压自行下降或自行加药。"]}
                level = "attention"
                messages.append("这次血压读数很高。若没有急性症状，至少间隔1分钟复测；仍然很高请立即联系医疗人员，并让家属接手。若伴胸痛、呼吸困难、新发无力、说话或视力异常，立即拨打120。不要自行加药。")
            comparisons = [("bp_systolic_high", systolic, "high"), ("bp_diastolic_high", diastolic, "high"),
                           ("bp_systolic_low", systolic, "low"), ("bp_diastolic_low", diastolic, "low")]
            outside = False
            for key, value, direction in comparisons:
                threshold = _finite_number(targets.get(key))
                if threshold is not None and ((direction == "high" and value > threshold) or (direction == "low" and value < threshold)):
                    outside = True
            if outside:
                level = "attention"
                messages.append("这次血压超出已录入的医生目标范围，请核对后按医嘱复测，并联系家属/医疗团队；不要凭单次读数自行停药或加药。")
    if not messages:
        messages.append("已收到待核对的数据。常规血压血糖目标按医生方案判断；没有触发本工具规则不代表没有健康风险。")
    return {"level": level, "messages": list(dict.fromkeys(messages))}


def offline_answer(question, profile=None):
    """Small, reviewed FAQ; no generated individual prescription or menu."""
    question = _clean(question)
    profile = profile or {}
    urgent = assess([], question, {})
    if urgent["level"] == "urgent":
        return "\n".join(urgent["messages"])
    if re.search(r"停药|加药|减药|剂量|胰岛素.*(?:多少|几|打)|药.*(?:几片|多少|怎么吃)|能否停|可以停", question):
        return "药物名称、剂量和服药时间请按出院医嘱或医生调整方案执行。我不能根据聊天或单次读数替您加减药。请把具体问题交给开药医生；若数值异常或身体不适，及时联系家属和医疗人员。"
    if re.search(r"呛|吞咽|稀粥|稀汤", question):
        return "脑梗后若喝水呛咳、吃饭咳嗽或吞咽后声音湿哑，应先联系医生做吞咽评估。食物软硬和稠度按评估结果选择，不能默认稀粥、稀汤更安全。若当前无法安全吞咽或意识异常，请拨打120，不要强行喂食喂水。"
    if re.search(r"低血糖|血糖低", question):
        return "血糖单位确认后，低于3.9 mmol/L需要及时处理。清醒且能安全吞咽时按医生预设的低血糖方案执行和复测；意识异常或无法安全吞咽时立即拨打120、通知家属，不要强行喂食。平时请让医生为您写好具体低血糖处理方案。"
    if re.search(r"水果|苹果|香蕉|西瓜|橙子|葡萄|果汁", question):
        return "水果不需要一概禁食，优先完整水果，通常不把果汁作为日常饮料。份量需要计入全天饮食安排，并结合自己的血糖反应。现在还没有完整的用药、肾功能和营养方案，我先不替您定具体克数；可把想吃的水果和份量发给家属/营养师确认。"
    if re.search(r"红薯|土豆|玉米|山药|馒头|米饭|主食|面条", question):
        return "主食要按计划定量。米饭、面条、馒头，以及土豆、红薯、玉米等都需要计入这一餐的主食安排，不能在原有米饭之外任意叠加，也不要为了控糖完全停掉主食。具体份量按营养方案执行。"
    if re.search(r"无糖|饼干|糕点|点心", question):
        return "“无糖”不等于可以不限量吃。饼干、糕点里的淀粉和油脂仍要计入饮食安排；查看营养成分表和每份量，避免边聊天边不断加量。"
    if re.search(r"盐|咸菜|腌|酱油|腊肉", question):
        return "日常逐步少放盐、少吃咸菜和加工肉，酱油及调味料里的盐也要算进去。不要自行改用高钾低钠盐：肾功能和用药还没明确，应先询问医生是否适合。"
    if re.search(r"酒|喝酒", question):
        return "糖尿病日常食养不推荐饮酒。脑梗后正在服药，更应把饮酒问题交给医生确认，别把酒当成活血或降糖办法。"
    if re.search(r"菜单|食谱|早饭|早餐|午饭|午餐|晚饭|晚餐|今天吃|每天吃", question):
        if profile.get("doctor_plan"):
            return "请优先执行家属已录入的医生饮食方案：\n" + str(profile["doctor_plan"])[:3000] + "\n具体份量、过敏和食物质地仍按原医嘱。这个离线助手暂不自行改动处方或生成新的定量菜单。"
        return "可以先记住配餐结构：定量主食＋蔬菜＋合适的蛋白质食物，清淡烹调。这只是一般搭配思路。正式的每日菜单和份量，还需要目前药名、体重、肾功能、过敏情况及医生饮食要求；资料未齐前不会自动生成个体化定量菜单。"
    if re.search(r"血压|量压|测压", question):
        return "按医生安排的时间测血压；测前安静坐5分钟，上臂与心脏同高、测量时不说话。每次两遍，间隔1分钟，保留两次原始读数。目标范围用医生给您的方案，不根据单次读数自行改药。"
    if re.search(r"血糖|空腹|餐后", question):
        return "记录血糖时请同时写数值、仪器单位和测量餐次，例如“空腹血糖7.2 mmol/L”。餐后2小时从开始吃第一口饭计时。测量频率和目标按医生方案；未知餐次的数据不会套用空腹目标。"
    return "我现在处于离线提示模式，可以帮您记录血压血糖，或解释主食、水果、无糖食品、少盐和测量方法。其他问题先交给家属/医疗团队；接通模型后也会保留医嘱和异常通知边界。"
