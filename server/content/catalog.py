"""Load and validate NANE's versioned Arabic content catalog."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple


DATA_DIR = Path(__file__).resolve().parent / "data"
PROFILE_FILE = "profile_questions.json"
QUESTION_FILE = "question_bank.json"
MYSTERY_FILE = "mystery_templates.json"


class ContentValidationError(ValueError):
    """Raised when content data is incomplete, inconsistent, or unsafe to use."""


@dataclass(frozen=True)
class ContentCatalog:
    profile_questions: Tuple[Dict[str, Any], ...]
    questions: Tuple[Dict[str, Any], ...]
    mystery_templates: Tuple[Dict[str, Any], ...]
    profile_fields: frozenset
    schema_version: int = 1

    def profile_question(self, field_id: str):
        for item in self.profile_questions:
            if item["field_id"] == field_id:
                return copy.deepcopy(item)
        return None

    def question(self, question_id: str):
        for item in self.questions:
            if item["id"] == question_id:
                return copy.deepcopy(item)
        return None


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except OSError as exc:
        raise ContentValidationError("تعذر فتح ملف المحتوى: %s" % path.name) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContentValidationError("ملف المحتوى ليس JSON صالحًا: %s" % path.name) from exc
    if not isinstance(value, dict):
        raise ContentValidationError("جذر ملف المحتوى يجب أن يكون كائنًا: %s" % path.name)
    if value.get("schema_version") != 1:
        raise ContentValidationError("إصدار ملف المحتوى غير مدعوم: %s" % path.name)
    return value


def _unique_ids(items: Iterable[Dict[str, Any]], label: str) -> None:
    seen = set()
    for item in items:
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ContentValidationError("%s يحتوي عنصرًا بلا معرّف صالح." % label)
        if identifier in seen:
            raise ContentValidationError("%s يحتوي معرّفًا مكررًا: %s" % (label, identifier))
        seen.add(identifier)


def _validate_options(options: Any, label: str, minimum: int = 4) -> None:
    if not isinstance(options, list) or len(options) < minimum:
        raise ContentValidationError("%s يحتاج إلى %d خيارات على الأقل." % (label, minimum))
    ids = set()
    texts = set()
    for option in options:
        if not isinstance(option, dict):
            raise ContentValidationError("خيار غير صالح في %s." % label)
        option_id = option.get("id")
        text = option.get("text_ar")
        if not isinstance(option_id, str) or not option_id:
            raise ContentValidationError("معرّف خيار مفقود في %s." % label)
        if not isinstance(text, str) or not text.strip():
            raise ContentValidationError("نص خيار مفقود في %s." % label)
        if option_id in ids or text.strip() in texts:
            raise ContentValidationError("خيارات مكررة في %s." % label)
        ids.add(option_id)
        texts.add(text.strip())


def load_catalogs(data_dir: Path = None) -> ContentCatalog:
    """Load all catalogs and enforce cross-file references before game use."""
    root = Path(data_dir) if data_dir is not None else DATA_DIR
    profile_data = _read_json(root / PROFILE_FILE)
    question_data = _read_json(root / QUESTION_FILE)
    mystery_data = _read_json(root / MYSTERY_FILE)

    profile = profile_data.get("questions")
    questions = question_data.get("questions")
    mysteries = mystery_data.get("templates")
    if not isinstance(profile, list) or len(profile) < 21:
        raise ContentValidationError("يجب توفير 21 سؤال ملف شخصية على الأقل.")
    if not isinstance(questions, list) or len(questions) < 150:
        raise ContentValidationError("يجب توفير 150 سؤال لعب على الأقل.")
    if not isinstance(mysteries, list) or len(mysteries) < 40:
        raise ContentValidationError("يجب توفير 40 قالب رسالة مجهولة على الأقل.")

    _unique_ids(profile, "أسئلة الملف الشخصي")
    _unique_ids(questions, "بنك الأسئلة")
    _unique_ids(mysteries, "قوالب الرسائل")

    fields = set()
    for item in profile:
        field_id = item.get("field_id")
        if not isinstance(field_id, str) or not field_id.strip() or field_id in fields:
            raise ContentValidationError("حقل ملف شخصي مفقود أو مكرر.")
        fields.add(field_id)
        if not isinstance(item.get("text_ar"), str) or not item["text_ar"].strip():
            raise ContentValidationError("نص سؤال ملف الشخصية غير صالح: %s" % item["id"])
        if item.get("required") is not True or item.get("answer_type") != "single_choice":
            raise ContentValidationError("أسئلة الملف يجب أن تكون إلزامية واختيارًا واحدًا.")
        _validate_options(item.get("options"), item["id"])

    categories = set()
    for item in questions:
        label = item["id"]
        if not isinstance(item.get("text_ar"), str) or not item["text_ar"].strip():
            raise ContentValidationError("نص السؤال غير صالح: %s" % label)
        category = item.get("category")
        if not isinstance(category, str) or not category:
            raise ContentValidationError("تصنيف السؤال مفقود: %s" % label)
        categories.add(category)
        if item.get("difficulty") not in (1, 2, 3):
            raise ContentValidationError("مستوى الصعوبة يجب أن يكون 1 أو 2 أو 3: %s" % label)
        _validate_options(item.get("options"), label)
        uses = item.get("use_in")
        if not isinstance(uses, list) or not {"player_question", "live_choice"}.issubset(set(uses)):
            raise ContentValidationError("السؤال يجب أن يدعم المرحلتين التفاعليتين: %s" % label)
        if not isinstance(item.get("estimated_seconds"), int) or item["estimated_seconds"] < 5:
            raise ContentValidationError("زمن السؤال غير صالح: %s" % label)
    if len(categories) < 12:
        raise ContentValidationError("يجب أن يغطي بنك الأسئلة 12 مجالًا على الأقل.")

    for template in mysteries:
        label = template["id"]
        required = template.get("required_fields")
        clues = template.get("clues")
        if not isinstance(required, list) or len(required) != 3 or len(set(required)) != 3:
            raise ContentValidationError("كل قالب رسالة يجب أن يعتمد على 3 حقائق مختلفة: %s" % label)
        if any(field not in fields for field in required):
            raise ContentValidationError("قالب الرسالة يشير إلى حقل غير موجود: %s" % label)
        if not isinstance(clues, list) or len(clues) != 3:
            raise ContentValidationError("كل قالب يحتاج ثلاثة تلميحات: %s" % label)
        if [clue.get("level") for clue in clues] != [1, 2, 3]:
            raise ContentValidationError("مستويات التلميح يجب أن تكون 1 ثم 2 ثم 3: %s" % label)
        if [clue.get("field_id") for clue in clues] != required:
            raise ContentValidationError("ترتيب التلميحات لا يطابق الحقول: %s" % label)
        for clue in clues:
            text = clue.get("text_template")
            if not isinstance(text, str) or "{value}" not in text:
                raise ContentValidationError("قالب التلميح يجب أن يحتوي {value}: %s" % label)
        if template.get("difficulty") not in (1, 2, 3):
            raise ContentValidationError("صعوبة قالب الرسالة غير صالحة: %s" % label)

    return ContentCatalog(
        profile_questions=tuple(profile),
        questions=tuple(questions),
        mystery_templates=tuple(mysteries),
        profile_fields=frozenset(fields),
        schema_version=1,
    )
