import unittest

from server.content import ContentEngine, ContentSelectionError, load_catalogs
from server.content.engine import score_correct_guess, score_final_guess, score_mystery_guess


def complete_profile(catalog):
    return {
        item["field_id"]: item["options"][0]["id"]
        for item in catalog.profile_questions
    }


class ContentEngineTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalogs()
        self.engine = ContentEngine(self.catalog, seed=73)
        self.answers = complete_profile(self.catalog)

    def test_difficulty_progresses_from_easy_to_hard(self):
        engine = ContentEngine(self.catalog, seed=1)
        self.assertEqual(engine.difficulty_for_round(1, 10), 1)
        self.assertEqual(engine.difficulty_for_round(5, 10), 2)
        self.assertEqual(engine.difficulty_for_round(10, 10), 3)

    def test_recent_pair_accuracy_adjusts_difficulty_by_only_one_level(self):
        engine = ContentEngine(self.catalog, seed=1)
        self.assertEqual(engine.difficulty_for_round(1, 10, [True, True, True]), 2)
        self.assertEqual(engine.difficulty_for_round(10, 10, [False, False, False]), 2)
        self.assertEqual(engine.difficulty_for_round(1, 10, [True]), 1)

    def test_invalid_round_or_difficulty_is_rejected(self):
        with self.assertRaises(ContentSelectionError):
            self.engine.difficulty_for_round(0, 8)
        with self.assertRaises(ContentSelectionError):
            self.engine.difficulty_for_round(9, 8)
        with self.assertRaises(ContentSelectionError):
            self.engine.choose_question(difficulty=4)

    def test_question_selection_is_deterministic_for_same_seed(self):
        first = ContentEngine(self.catalog, seed=900).choose_question(difficulty=2)
        second = ContentEngine(self.catalog, seed=900).choose_question(difficulty=2)
        self.assertEqual(first["id"], second["id"])

    def test_question_selection_never_reuses_used_ids(self):
        used = set()
        for _ in range(15):
            question = self.engine.choose_question(used_question_ids=used, difficulty=1)
            self.assertNotIn(question["id"], used)
            used.add(question["id"])
        self.assertEqual(len(used), 15)

    def test_question_selection_falls_back_to_nearest_available_level(self):
        hardest_ids = {
            item["id"] for item in self.catalog.questions
            if item["difficulty"] == 3
        }
        question = self.engine.choose_question(used_question_ids=hardest_ids, difficulty=3)
        self.assertEqual(question["difficulty"], 2)

    def test_question_pool_exhaustion_is_reported_not_repeated(self):
        all_ids = {item["id"] for item in self.catalog.questions}
        with self.assertRaises(ContentSelectionError):
            self.engine.choose_question(used_question_ids=all_ids)

    def test_returned_question_is_a_copy_not_mutable_catalog_state(self):
        question = self.engine.choose_question()
        original = self.catalog.question(question["id"])["options"][0]["text_ar"]
        question["options"][0]["text_ar"] = "تعديل اختباري"
        self.assertEqual(self.catalog.question(question["id"])["options"][0]["text_ar"], original)

    def test_profile_fact_excludes_revealed_fields_and_keeps_answer_private(self):
        revealed = {"favorite_color", "favorite_meal", "favorite_dessert"}
        prompt = self.engine.choose_profile_fact("p2", self.answers, revealed_fields=revealed)
        self.assertNotIn(prompt["field_id"], revealed)
        self.assertIn("private_answer", prompt)
        public = self.engine.public_profile_prompt(prompt)
        self.assertNotIn("private_answer", public)
        self.assertNotIn("field_id", public)
        self.assertNotIn("private_answer", public["question"])

    def test_profile_fact_rejects_when_all_fields_are_revealed(self):
        with self.assertRaises(ContentSelectionError):
            self.engine.choose_profile_fact("p2", self.answers, revealed_fields=self.catalog.profile_fields)

    def test_profile_fact_rejects_bad_answer_values_without_leaking_them(self):
        bad = dict(self.answers)
        bad["favorite_color"] = "not-an-option"
        # Other valid fields remain eligible; malformed fields are ignored.
        with self.assertRaises(ContentSelectionError) as raised:
            self.engine.choose_profile_fact(
                "p2", bad, revealed_fields=self.catalog.profile_fields - {"favorite_color"}
            )
        self.assertNotIn("not-an-option", str(raised.exception))

    def test_target_selection_excludes_asker_and_avoids_previous_target(self):
        target = self.engine.choose_target(
            ["p1", "p2", "p3"], excluded_player_ids={"p1"}, last_target_id="p2"
        )
        self.assertEqual(target, "p3")

    def test_target_selection_balances_least_used_eligible_player(self):
        target = self.engine.choose_target(
            ["p1", "p2", "p3"],
            excluded_player_ids={"p1"},
            target_counts={"p2": 4, "p3": 1},
        )
        self.assertEqual(target, "p3")

    def test_target_selection_fails_when_everyone_is_excluded(self):
        with self.assertRaises(ContentSelectionError):
            self.engine.choose_target(["p1", "p2"], excluded_player_ids={"p1", "p2"})

    def test_question_routing_never_targets_the_asker(self):
        question = self.catalog.questions[0]
        routed = self.engine.route_player_question(question["id"], "p1", ["p1", "p2", "p3"])
        self.assertEqual(routed["asker_player_id"], "p1")
        self.assertIn(routed["target_player_id"], {"p2", "p3"})
        public = self.engine.public_routed_question(routed)
        self.assertNotIn("target_player_id", public)
        self.assertNotIn("private_target", public)
        self.assertEqual(public["asker_player_id"], "p1")
        self.assertTrue(self.engine.private_target_status(routed, routed["target_player_id"])["is_target"])
        self.assertFalse(self.engine.private_target_status(routed, "p1")["is_target"])

    def test_question_routing_rejects_asker_missing_from_room(self):
        question = self.catalog.questions[0]
        with self.assertRaises(ContentSelectionError):
            self.engine.route_player_question(question["id"], "not-in-room", ["p1", "p2"])

    def test_question_routing_rejects_unknown_question(self):
        with self.assertRaises(ContentSelectionError):
            self.engine.route_player_question("not-a-question", "p1", ["p1", "p2"])

    def test_mystery_has_three_contextual_clues_and_private_answers(self):
        mystery = self.engine.build_mystery("p3", self.answers, difficulty=2)
        self.assertEqual(len(mystery["clues"]), 3)
        self.assertEqual(set(mystery["private_answers"]), set(clue["field_id"] for clue in mystery["clues"]))
        for clue in mystery["clues"]:
            self.assertNotIn("{value}", clue["text_ar"])
            self.assertIn(mystery["private_answers"][clue["field_id"]], clue["text_ar"])

    def test_public_mystery_releases_only_requested_clues_and_hides_identity(self):
        mystery = self.engine.build_mystery("secret-target", self.answers)
        public = self.engine.public_mystery(mystery, revealed_clue_count=1)
        self.assertEqual(len(public["revealed_clues"]), 1)
        self.assertNotIn("target_player_id", public)
        self.assertNotIn("private_answers", public)
        self.assertNotIn("field_id", public["revealed_clues"][0])
        self.assertNotIn("private_answers", public["revealed_clues"][0])

    def test_mystery_excludes_previously_revealed_profile_facts(self):
        revealed = {"favorite_color", "favorite_meal", "favorite_dessert"}
        mystery = self.engine.build_mystery("p3", self.answers, revealed_fields=revealed)
        used_fields = {clue["field_id"] for clue in mystery["clues"]}
        self.assertTrue(used_fields.isdisjoint(revealed))

    def test_mystery_templates_are_not_reused(self):
        used = {item["id"] for item in self.catalog.mystery_templates}
        with self.assertRaises(ContentSelectionError):
            self.engine.build_mystery("p3", self.answers, used_template_ids=used)

    def test_mystery_solution_is_separate_from_public_projection(self):
        mystery = self.engine.build_mystery("p3", self.answers)
        solution = self.engine.mystery_solution(mystery)
        self.assertEqual(solution["target_player_id"], "p3")
        public = self.engine.public_mystery(mystery, 3)
        self.assertNotIn("target_player_id", public)
        self.assertEqual(len(public["revealed_clues"]), 3)

    def test_pair_knowledge_is_recorded_per_direction(self):
        self.assertIsNone(self.engine.pair_accuracy("p1", "p2"))
        self.engine.record_guess("p1", "p2", True)
        self.engine.record_guess("p1", "p2", False)
        self.engine.record_guess("p1", "p2", True)
        self.assertAlmostEqual(self.engine.pair_accuracy("p1", "p2"), 2.0 / 3.0)
        self.assertIsNone(self.engine.pair_accuracy("p2", "p1"))

    def test_pair_history_rejects_self_guesses_and_bad_results(self):
        with self.assertRaises(ContentSelectionError):
            self.engine.record_guess("p1", "p1", True)
        with self.assertRaises(ContentSelectionError):
            self.engine.record_guess("p1", "p2", "yes")

    def test_scoring_rules_are_small_and_predictable(self):
        self.assertEqual(score_correct_guess(), 2)
        self.assertEqual(score_mystery_guess(1), 3)
        self.assertEqual(score_mystery_guess(2), 2)
        self.assertEqual(score_mystery_guess(3), 1)
        self.assertEqual(score_final_guess(), 3)
        with self.assertRaises(ContentSelectionError):
            score_mystery_guess(4)


if __name__ == "__main__":
    unittest.main()
