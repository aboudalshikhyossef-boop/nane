"""Five-phase match planning and task construction for NANE."""
from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..content import ContentEngine, ContentSelectionError, load_catalogs


PHASES = ("knowledge", "mystery", "player_question", "live_choice", "finale")
PHASE_LABELS = {
    "knowledge": "من يعرفني؟",
    "mystery": "الرسالة المجهولة",
    "player_question": "سؤال من لاعب",
    "live_choice": "من اختار هذا؟",
    "finale": "المواجهة الأخيرة",
}


class MatchPlanningError(ValueError):
    """Raised when a fair set of phase tasks cannot be prepared."""


def _pair_accuracy_history(state: Mapping[str, Any], target_id: str) -> List[bool]:
    result = []
    stats = state.get("pair_stats", {})
    for key, values in stats.items():
        if key.endswith("|" + target_id) and isinstance(values, list):
            result.extend(bool(value) for value in values[-10:])
    return result


def _difficulty(engine: ContentEngine, phase_index: int, state: Mapping[str, Any],
                target_id: Optional[str] = None) -> int:
    history = _pair_accuracy_history(state, target_id) if target_id else None
    return engine.difficulty_for_round(phase_index + 1, len(PHASES), history)


def _profile_prompt(engine: ContentEngine, target: Mapping[str, Any],
                    profile_answers: Mapping[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    target_id = target["id"]
    revealed = state.setdefault("revealed_fields", {}).setdefault(target_id, [])
    field_counts = state.setdefault("profile_field_counts", {}).setdefault(target_id, {})
    prompt = engine.choose_profile_fact(
        target_id,
        profile_answers,
        revealed_fields=revealed,
        field_counts=field_counts,
    )
    revealed.append(prompt["field_id"])
    field_counts[prompt["field_id"]] = int(field_counts.get(prompt["field_id"], 0)) + 1
    return prompt


def build_phase_tasks(phase: str, players: Sequence[Mapping[str, Any]],
                      profiles: Mapping[str, Mapping[str, Any]],
                      state: Dict[str, Any], seed: int,
                      catalog=None) -> List[Dict[str, Any]]:
    """Build one phase at a time so later phases adapt to earlier match results."""
    if phase not in PHASES:
        raise MatchPlanningError("مرحلة المباراة غير معروفة.")
    if not 3 <= len(players) <= 4:
        raise MatchPlanningError("تحتاج المباراة إلى ثلاثة أو أربعة لاعبين.")
    catalog = catalog or load_catalogs()
    phase_index = PHASES.index(phase)
    selector = ContentEngine(catalog, seed=seed + (phase_index + 1) * 1009)
    tasks: List[Dict[str, Any]] = []
    ids = [player["id"] for player in players]
    names = {player["id"]: player["display_name"] for player in players}
    state.setdefault("used_question_ids", [])
    state.setdefault("used_template_ids", [])
    state.setdefault("revealed_fields", {})
    state.setdefault("target_counts", {})
    state.setdefault("last_target_id", None)
    state.setdefault("pair_stats", {})

    def append_task(kind: str, public: Dict[str, Any], private: Dict[str, Any],
                    eligible: Sequence[str], duration: int, **extra: Any) -> None:
        task = {
            "phase": phase,
            "kind": kind,
            "public": copy.deepcopy(public),
            "private": copy.deepcopy(private),
            "eligible_guesser_ids": list(eligible),
            "duration_seconds": duration,
            "status": "pending",
        }
        task.update(extra)
        tasks.append(task)

    if phase == "knowledge":
        for target in players:
            target_id = target["id"]
            answers = profiles[target_id]
            for _ in range(3):
                prompt = _profile_prompt(selector, target, answers, state)
                append_task(
                    "knowledge",
                    {
                        "target_name": names[target_id],
                        "question": prompt["question"],
                    },
                    {
                        "target_player_id": target_id,
                        "field_id": prompt["field_id"],
                        "correct_option_id": prompt["private_answer"]["option_id"],
                        "correct_answer_text": prompt["private_answer"]["text_ar"],
                    },
                    [player_id for player_id in ids if player_id != target_id],
                    75,
                )

    elif phase == "mystery":
        for target in players:
            target_id = target["id"]
            for _ in range(2):
                difficulty = _difficulty(selector, phase_index, state, target_id)
                mystery = selector.build_mystery(
                    target_id,
                    profiles[target_id],
                    revealed_fields=state["revealed_fields"].get(target_id, []),
                    used_template_ids=state["used_template_ids"],
                    difficulty=difficulty,
                )
                state["used_template_ids"].append(mystery["template_id"])
                for clue in mystery["clues"]:
                    state["revealed_fields"].setdefault(target_id, []).append(clue["field_id"])
                append_task(
                    "mystery",
                    {
                        "title_ar": mystery["title_ar"],
                        "story_ar": mystery["story_ar"],
                        "difficulty": mystery["difficulty"],
                    },
                    {"mystery": mystery},
                    [player_id for player_id in ids if player_id != target_id],
                    30,
                    clue_level=1,
                )

    elif phase == "player_question":
        for asker in players:
            asker_id = asker["id"]
            for _ in range(2):
                # The target's knowledge of the asker is used to adjust complexity.
                candidates = [player_id for player_id in ids if player_id != asker_id]
                target_counts = state["target_counts"]
                target_id = selector.choose_target(
                    candidates,
                    target_counts=target_counts,
                    last_target_id=state.get("last_target_id"),
                )
                difficulty = _difficulty(selector, phase_index, state, target_id)
                question = selector.choose_question(
                    used_question_ids=state["used_question_ids"],
                    difficulty=difficulty,
                    allowed_use="player_question",
                )
                state["used_question_ids"].append(question["id"])
                state["target_counts"][target_id] = int(target_counts.get(target_id, 0)) + 1
                state["last_target_id"] = target_id
                append_task(
                    "player_question",
                    {
                        "asker_name": names[asker_id],
                        "question": question,
                        "target_answers_allowed": True,
                    },
                    {
                        "asker_player_id": asker_id,
                        "target_player_id": target_id,
                        "answer_option_id": None,
                        "answer_text": None,
                    },
                    [player_id for player_id in ids if player_id != target_id],
                    60,
                    subphase="answer",
                )

    elif phase == "live_choice":
        group_history = []
        for values in state["pair_stats"].values():
            if isinstance(values, list):
                group_history.extend(bool(value) for value in values[-10:])
        for _ in range(3):
            difficulty = selector.difficulty_for_round(phase_index + 1, len(PHASES), group_history)
            question = selector.choose_live_question(
                used_question_ids=state["used_question_ids"],
                difficulty=difficulty,
            )
            state["used_question_ids"].append(question["id"])
            append_task(
                "live_choice",
                {"question": question},
                {"answers": {}, "target_order": list(ids)},
                list(ids),
                60,
                subphase="answer",
                live_target_index=0,
            )

    elif phase == "finale":
        for target in players:
            target_id = target["id"]
            answers = profiles[target_id]
            for _ in range(2):
                first = _profile_prompt(selector, target, answers, state)
                second = _profile_prompt(selector, target, answers, state)
                answer_a = first["private_answer"]["text_ar"]
                answer_b = second["private_answer"]["text_ar"]
                prompt = (
                    "هناك شخص يفضّل «%s»، وعندما يختار %s يميل إلى «%s». "
                    "من صاحب هذه الاختيارات؟"
                ) % (answer_a, second["question"]["text_ar"], answer_b)
                append_task(
                    "finale",
                    {"text_ar": prompt, "difficulty": 3},
                    {
                        "target_player_id": target_id,
                        "facts": [
                            {"field_id": first["field_id"], "answer": answer_a},
                            {"field_id": second["field_id"], "answer": answer_b},
                        ],
                    },
                    [player_id for player_id in ids if player_id != target_id],
                    70,
                )

    if not tasks:
        raise MatchPlanningError("تعذر إنشاء مهام لهذه المرحلة.")
    return tasks
