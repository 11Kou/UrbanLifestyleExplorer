import copy
from collections import Counter
import unittest
from unittest.mock import patch

from test import check_response, load_cases
from urban_explorer.models import AgentResult, ChatRequest, ChatResponse, ExploreFilters, Place, SessionState


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.case = {"expect": {"city": "成都", "agents": ["food"],
                               "filters": {"budget": 50, "budget_scope": "food"}, "results": "required"}}
        place = Place(id="amap-fixture", city="成都", name="测试餐厅", kind="food", category="中餐厅", cost=35, mode="live")
        self.body = ChatResponse(
            answer="测试回答", mode="live", agents=["food"], notices=[],
            session=SessionState(filters=ExploreFilters(budget=50), budgets={"food": 50, "fun": None}),
            results=[AgentResult(agent="food", summary="测试", places=[place])],
        ).model_dump(mode="json")

    def test_dataset_has_40_distinct_cases_and_expected_coverage(self):
        cases = load_cases()
        self.assertEqual(len(cases), 40)
        self.assertEqual(len({case["id"] for case in cases}), 40)
        self.assertEqual(Counter(case["category"] for case in cases), {
            "城市认知": 6, "需求分流": 8, "条件筛选": 8, "连续追问": 10, "边界输入": 8,
        })
        for case in cases:
            expected = case["expect"]
            with self.subTest(case=case["id"]):
                self.assertIn(expected.get("results", "allow_empty"), ("allow_empty", "required", "empty"))
                if expected.get("status", 200) == 200:
                    ChatRequest.model_validate({"session_id": "evaluation_validation_session", "message": case["question"], **case.get("payload", {})})
                    self.assertTrue(expected["city"])
                    self.assertTrue(set(expected["agents"]) <= {"city", "food", "fun"})
                    self.assertTrue(set(expected.get("filters", {})) <= ExploreFilters.model_fields.keys())
        followups = [case for case in cases if case["category"] == "连续追问"]
        self.assertEqual(Counter(c["session"] for c in followups), {"follow-food": 5, "follow-mixed": 5})

    def test_grader_rejects_over_budget_unknown_prices_and_wrong_city(self):
        self.assertEqual(check_response(self.case, 200, self.body), [])
        for changes in ({"cost": 51}, {"cost": None}, {"city": "杭州"}, {"id": "demo-fake"}, {"kind": "fun"}):
            with self.subTest(changes=changes):
                body = copy.deepcopy(self.body)
                body["results"][0]["places"][0].update(changes)
                self.assertTrue(check_response(self.case, 200, body))

    def test_grader_checks_cleared_budget_and_removed_preference(self):
        case = {"expect": {"city": "成都", "agents": ["food"], "filters": {"budget": None},
                           "budgets": {"food": None}, "preferences_excludes": ["不吃辣"]}}
        self.assertTrue(check_response(case, 200, self.body))
        self.body["session"]["filters"]["budget"] = None
        self.body["session"]["budgets"]["food"] = None
        self.assertEqual(check_response(case, 200, self.body), [])
        self.body["session"]["filters"]["preferences"] = ["不吃辣"]
        self.assertTrue(check_response(case, 200, self.body))

    def test_empty_results_have_explicit_policies(self):
        self.body["results"][0]["places"] = []
        self.assertTrue(check_response(self.case, 200, self.body))
        self.case["expect"]["results"] = "allow_empty"
        self.assertEqual(check_response(self.case, 200, self.body), [])
        self.case["expect"]["results"] = "empty"
        self.assertEqual(check_response(self.case, 200, self.body), [])

    def test_place_type_does_not_pass_only_because_agent_is_correct(self):
        self.case["expect"]["place_terms"] = {"food": ["火锅"]}
        self.assertTrue(check_response(self.case, 200, self.body))
        self.body["results"][0]["places"][0]["category"] = "火锅店"
        self.assertEqual(check_response(self.case, 200, self.body), [])

    def test_expected_validation_errors_do_not_mask_service_failures(self):
        case = {"expect": {"status": 422}}
        self.assertEqual(check_response(case, 422, {"detail": []}), [])
        self.assertTrue(check_response(case, 500, {"detail": "service failure"}))

    def test_followup_is_blocked_after_failed_setup(self):
        from pathlib import Path
        from types import SimpleNamespace
        from test import evaluate_live

        cases = [
            {"id": f"case-{i}", "session": "shared", "category": "连续追问", "question": "测试",
             "expect": {"city": "成都", "agents": ["food"]}}
            for i in (1, 2)
        ]
        settings = SimpleNamespace(amap_api_key=SimpleNamespace(get_secret_value=lambda: "test-only"), has_llm=True)
        with patch("test.load_cases", return_value=cases), patch("urban_explorer.config.Settings", return_value=settings), \
             patch("urban_explorer.api.create_app"), patch("fastapi.testclient.TestClient") as client:
            connection = client.return_value.__enter__.return_value
            connection.post.return_value.status_code = 502
            connection.post.return_value.json.return_value = {"detail": "测试失败"}
            records = evaluate_live(Path("unused-evaluation-test.db"))
        self.assertFalse(records[0]["passed"])
        self.assertTrue(records[1]["blocked"])
        self.assertEqual(connection.post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
