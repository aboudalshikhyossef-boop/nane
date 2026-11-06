"""Deterministic, server-side content selection for NANE.

The engine never sends private answers itself. Callers must keep the returned
private fields on the server and use the public projection helpers for clients.
"""
from __future__ import annotations

import copy
import random
import threading
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .catalog import ContentCatalog, load_catalogs


class ContentSelectionError(ValueError):
    """Raised when the current game state has no fair content choice."""


class ContentEngine:
    def __init__(self, catalog: Optional[ContentCatalog] = None, seed: Optional[int] = None):
        self.catalog = catalog or load_catalogs()
        self._rng = random.Random(seed)
        self._lock = threading.RLock()
        self._pair_history = defaultdict(lambda: deque(maxlen=10))

    @staticmethod
    def _validate_difficulty(difficulty: int) -> None:
        if difficulty not in (1, 2, 3):
            raise ContentSelectionError("مستوى الصعوبة يجب أن يكون بين 1 و3.")

    @staticmethod
    def _weighted_pick(items: Sequence[Dict[str, Any]], weights: Sequence[float],
                       rng: random.Random) -> Dict[str, Any]:
        if not items:
            raise ContentSelectionError("لا يوجد محتوى مناسب للحالة الحالية.")
        return rng.choices(list(items), weights=list(weights), k=1)[0]

    @staticmethod
    def _answer_text(profile_question: Dict[str, Any], answer: Any) -> Tuple[str, Optional[str]]:
        options = profile_question["options"]
        if isinstance(answer, dict):
            option_id = answer.get("option_id") or answer.get("id")
            text = answer.get("text_ar") or answer.get("text")
            if option_id:
                for option in options:
                    if option["id"] == option_id:
                        return option["text_ar"], option["id"]
            if isinstance(text, str) and text.strip():
                for option in options:
                    if option["text_ar"] == text.strip():
                        return option["text_ar"], option["id"]
                raise ContentSelectionError("إجابة الملف لا تطابق خيارات السؤال.")
        elif isinstance(answer, str):
            value = answer.strip()
            for option in options:
                if option["id"] == value or option["text_ar"] == value:
                    return option["text_ar"], option["id"]
        raise ContentSelectionError("إجابة ملف الشخصية غير صالحة للسؤال %s." % profile_question["id"])

    @staticmethod
    def _accuracy(history: Optional[Iterable[Any]]) -> Optional[float]:
        if history is None:
            return None
        values = list(history)
        if len(values) < 3:
            return None
        normalized = []
        for value in values:
            if isinstance(value, bool):
                normalized.append(1.0 if value else 0.0)
            elif isinstance(value, (int, float)) and 0.0 <= float(value) <= 1.0:
                normalized.append(float(value))
            else:
                raise ContentSelectionError("سجل الدقة يحتوي قيمة غير صالحة.")
        return sum(normalized) / float(len(normalized))

    def difficulty_for_round(self, round_number: int, total_rounds: int,
                             recent_accuracy: Optional[Iterable[Any]] = None) -> int:
        """Raise base difficulty by match progress, then adjust one step for recent accuracy."""
        if not isinstance(round_number, int) or not isinstance(total_rounds, int):
            raise ContentSelectionError("رقم الجولة يجب أن يكون عددًا صحيحًا.")
        if total_rounds < 1 or round_number < 1 or round_number > total_rounds:
            raise ContentSelectionError("رقم الجولة خارج نطاق المباراة.")
        progress = 0.0 if total_rounds == 1 else (round_number - 1) / float(total_rounds - 1)
        if progress < 0.30:
            level = 1
        elif progress < 0.72:
            level = 2
        else:
            level = 3
        accuracy = self._accuracy(recent_accuracy)
        if accuracy is not None:
            if accuracy >= 0.80 and level < 3:
                level += 1
            elif accuracy <= 0.35 and level > 1:
                level -= 1
        return level

    def choose_question(self, used_question_ids: Iterable[str] = (), difficulty: int = 1,
                        allowed_use: str = "player_question",
                        category_counts: Optional[Mapping[str, int]] = None,
                        recent_categories: Iterable[str] = ()) -> Dict[str, Any]:
        """Choose an unused bank question, balancing category exposure and difficulty."""
        self._validate_difficulty(difficulty)
        used = set(used_question_ids or ())
        recent = set(recent_categories or ())
        counts = category_counts or {}
        candidates = [
            item for item in self.catalog.questions
            if item["id"] not in used and allowed_use in item["use_in"]
        ]
        if not candidates:
            raise ContentSelectionError("انتهت الأسئلة المتاحة لهذه المرحلة؛ لا نكرر سؤالًا مكشوفًا.")
        exact = [item for item in candidates if item["difficulty"] == difficulty]
        if exact:
            candidates = exact
        else:
            distance = min(abs(item["difficulty"] - difficulty) for item in candidates)
            candidates = [item for item in candidates if abs(item["difficulty"] - difficulty) == distance]
        weights = []
        for item in candidates:
            category = item["category"]
            exposure = max(0, int(counts.get(category, 0)))
            weight = 1.0 / (1.0 + exposure)
            if category in recent:
                weight *= 0.35
            weights.append(weight)
        with self._lock:
            chosen = self._weighted_pick(candidates, weights, self._rng)
        return copy.deepcopy(chosen)

    def choose_live_question(self, used_question_ids: Iterable[str] = (), difficulty: int = 1,
                             category_counts: Optional[Mapping[str, int]] = None,
                             recent_categories: Iterable[str] = ()) -> Dict[str, Any]:
        return self.choose_question(
            used_question_ids=used_question_ids,
            difficulty=difficulty,
            allowed_use="live_choice",
            category_counts=category_counts,
            recent_categories=recent_categories,
        )

    def choose_profile_fact(self, target_player_id: str,
                            profile_answers: Mapping[str, Any],
                            revealed_fields: Iterable[str] = (),
                            field_counts: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
        """Select an unrevealed, answered profile fact and retain its answer privately."""
        if not target_player_id or not isinstance(profile_answers, Mapping):
            raise ContentSelectionError("بيانات ملف اللاعب غير مكتملة.")
        revealed = set(revealed_fields or ())
        counts = field_counts or {}
        candidates = []
        for item in self.catalog.profile_questions:
            field_id = item["field_id"]
            if field_id in revealed or field_id not in profile_answers:
                continue
            try:
                answer_text, option_id = self._answer_text(item, profile_answers[field_id])
            except ContentSelectionError:
                continue
            candidates.append((item, answer_text, option_id, max(0, int(counts.get(field_id, 0)))))
        if not candidates:
            raise ContentSelectionError("لا توجد حقيقة شخصية صالحة وغير مكشوفة لهذا اللاعب.")
        min_used = min(candidate[3] for candidate in candidates)
        balanced = [candidate for candidate in candidates if candidate[3] == min_used]
        with self._lock:
            item, answer_text, option_id, _ = self._rng.choice(balanced)
        return {
            "target_player_id": target_player_id,
            "field_id": item["field_id"],
            "profile_question_id": item["id"],
            "question": {
                "id": item["id"],
                "text_ar": item["text_ar"],
                "category": item["category"],
                "options": copy.deepcopy(item["options"]),
            },
            "private_answer": {"option_id": option_id, "text_ar": answer_text},
        }

    @staticmethod
    def public_profile_prompt(prompt: Mapping[str, Any]) -> Dict[str, Any]:
        """Safe projection for clients; strips field IDs and the correct answer."""
        question = prompt.get("question")
        if not isinstance(question, Mapping):
            raise ContentSelectionError("بيانات السؤال غير صالحة.")
        return {
            "target_player_id": prompt.get("target_player_id"),
            "question": copy.deepcopy(dict(question)),
        }

    def choose_target(self, player_ids: Sequence[str],
                      excluded_player_ids: Iterable[str] = (),
                      target_counts: Optional[Mapping[str, int]] = None,
                      last_target_id: Optional[str] = None) -> str:
        """Choose a least-used eligible target, avoiding the previous target when possible."""
        normalized = []
        for player in player_ids:
            player_id = player.get("id") if isinstance(player, Mapping) else player
            if not isinstance(player_id, str) or not player_id:
                raise ContentSelectionError("قائمة اللاعبين تحتوي معرّفًا غير صالح.")
            if player_id not in normalized:
                normalized.append(player_id)
        excluded = set(excluded_player_ids or ())
        candidates = [player_id for player_id in normalized if player_id not in excluded]
        if not candidates:
            raise ContentSelectionError("لا يوجد لاعب مؤهل ليكون الهدف.")
        if last_target_id in candidates and len(candidates) > 1:
            candidates.remove(last_target_id)
        counts = target_counts or {}
        minimum = min(max(0, int(counts.get(player_id, 0))) for player_id in candidates)
        balanced = [
            player_id for player_id in candidates
            if max(0, int(counts.get(player_id, 0))) == minimum
        ]
        with self._lock:
            return self._rng.choice(balanced)

    def route_player_question(self, question_id: str, asker_player_id: str,
                              player_ids: Sequence[str],
                              target_counts: Optional[Mapping[str, int]] = None,
                              last_target_id: Optional[str] = None) -> Dict[str, Any]:
        """Route a player's chosen question to another player without exposing the target early."""
        question = self.catalog.question(question_id)
        if question is None or "player_question" not in question["use_in"]:
            raise ContentSelectionError("السؤال المختار غير موجود أو غير صالح لهذه المرحلة.")
        target = self.choose_target(
            player_ids,
            excluded_player_ids={asker_player_id},
            target_counts=target_counts,
            last_target_id=last_target_id,
        )
        return {
            "question": copy.deepcopy(question),
            "asker_player_id": asker_player_id,
            "target_player_id": target,
        }

    @staticmethod
    def public_routed_question(route: Mapping[str, Any]) -> Dict[str, Any]:
        """Expose the chosen prompt and asker, but never the secretly routed target."""
        question = route.get("question")
        if not isinstance(question, Mapping):
            raise ContentSelectionError("بيانات السؤال الموجّه غير صالحة.")
        public_question = {
            key: copy.deepcopy(question[key])
            for key in ("id", "text_ar", "category", "category_name_ar",
                        "difficulty", "options", "estimated_seconds")
            if key in question
        }
        return {
            "asker_player_id": route.get("asker_player_id"),
            "question": public_question,
        }

    @staticmethod
    def private_target_status(route: Mapping[str, Any], viewer_player_id: str) -> Dict[str, bool]:
        """Tell only the current viewer whether they are the secret respondent."""
        target = route.get("target_player_id")
        return {"is_target": bool(viewer_player_id and viewer_player_id == target)}

    def build_mystery(self, target_player_id: str,
                      profile_answers: Mapping[str, Any],
                      revealed_fields: Iterable[str] = (),
                      used_template_ids: Iterable[str] = (),
                      difficulty: int = 1) -> Dict[str, Any]:
        """Build a server-private mystery from three distinct, unrevealed profile facts."""
        self._validate_difficulty(difficulty)
        if not target_player_id or not isinstance(profile_answers, Mapping):
            raise ContentSelectionError("بيانات ملف اللاعب غير مكتملة.")
        revealed = set(revealed_fields or ())
        used_templates = set(used_template_ids or ())
        answer_values = {}
        for field_id, answer in profile_answers.items():
            profile_question = self.catalog.profile_question(field_id)
            if profile_question is not None and field_id not in revealed:
                try:
                    answer_values[field_id] = self._answer_text(profile_question, answer)[0]
                except ContentSelectionError:
                    continue
        candidates = [
            item for item in self.catalog.mystery_templates
            if item["id"] not in used_templates
            and all(field in answer_values for field in item["required_fields"])
        ]
        if not candidates:
            raise ContentSelectionError("لا يوجد قالب رسالة بثلاث حقائق متاحة وغير مكشوفة.")
        exact = [item for item in candidates if item["difficulty"] == difficulty]
        if exact:
            candidates = exact
        else:
            distance = min(abs(item["difficulty"] - difficulty) for item in candidates)
            candidates = [item for item in candidates if abs(item["difficulty"] - difficulty) == distance]
        with self._lock:
            template = copy.deepcopy(self._rng.choice(candidates))
        clues = []
        private_answers = {}
        for clue in template["clues"]:
            field_id = clue["field_id"]
            answer = answer_values[field_id]
            clues.append({
                "level": clue["level"],
                "field_id": field_id,
                "text_ar": clue["text_template"].replace("{value}", answer),
            })
            private_answers[field_id] = answer
        return {
            "template_id": template["id"],
            "target_player_id": target_player_id,
            "title_ar": template["title_ar"],
            "story_ar": template["story_ar"],
            "difficulty": template["difficulty"],
            "clues": clues,
            "private_answers": private_answers,
        }

    @staticmethod
    def public_mystery(mystery: Mapping[str, Any], revealed_clue_count: int = 1) -> Dict[str, Any]:
        """Return only released clues; never expose target, source fields, or answer map."""
        if revealed_clue_count not in (0, 1, 2, 3):
            raise ContentSelectionError("عدد التلميحات المكشوفة غير صالح.")
        clues = mystery.get("clues")
        if not isinstance(clues, list) or len(clues) != 3:
            raise ContentSelectionError("رسالة الغموض غير صالحة.")
        return {
            "title_ar": mystery.get("title_ar"),
            "story_ar": mystery.get("story_ar"),
            "difficulty": mystery.get("difficulty"),
            "revealed_clues": [
                {"level": clue["level"], "text_ar": clue["text_ar"]}
                for clue in clues[:revealed_clue_count]
            ],
        }

    @staticmethod
    def mystery_solution(mystery: Mapping[str, Any]) -> Dict[str, Any]:
        """Reveal target and supporting facts only after the round has closed."""
        return {
            "target_player_id": mystery.get("target_player_id"),
            "answers": copy.deepcopy(mystery.get("private_answers", {})),
        }

    def record_guess(self, guesser_player_id: str, target_player_id: str, correct: bool) -> None:
        if not guesser_player_id or not target_player_id or guesser_player_id == target_player_id:
            raise ContentSelectionError("لا يمكن تسجيل تخمين اللاعب لنفسه.")
        if not isinstance(correct, bool):
            raise ContentSelectionError("نتيجة التخمين يجب أن تكون صحيحة أو خاطئة.")
        with self._lock:
            self._pair_history[(guesser_player_id, target_player_id)].append(correct)

    def pair_accuracy(self, guesser_player_id: str, target_player_id: str) -> Optional[float]:
        with self._lock:
            history = list(self._pair_history.get((guesser_player_id, target_player_id), ()))
        if len(history) < 3:
            return None
        return sum(1 for value in history if value) / float(len(history))

    def difficulty_for_pair(self, round_number: int, total_rounds: int,
                            guesser_player_id: str, target_player_id: str) -> int:
        with self._lock:
            history = list(self._pair_history.get((guesser_player_id, target_player_id), ()))
        return self.difficulty_for_round(round_number, total_rounds, history)


def score_correct_guess() -> int:
    return 2


def score_mystery_guess(clue_level: int) -> int:
    if clue_level not in (1, 2, 3):
        raise ContentSelectionError("مستوى التلميح يجب أن يكون 1 أو 2 أو 3.")
    return 4 - clue_level


def score_final_guess() -> int:
    return 3
