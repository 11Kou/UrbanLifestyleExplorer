import unittest

from pydantic import ValidationError

from urban_explorer.catalog import DemoCatalog
from urban_explorer.models import ExploreFilters, SessionState
from urban_explorer.planning import rule_plan
from urban_explorer.recommendations import distance_m, filter_places


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = DemoCatalog()

    def test_budget_excludes_unknown_and_over_budget(self):
        filters = ExploreFilters(kind="fun", budget=50, budget_scope="fun")
        results = filter_places(self.catalog.places, filters)
        self.assertTrue(results)
        self.assertTrue(all(p.cost is not None and p.cost <= 50 for p in results))
        self.assertNotIn("demo-fun-6", {p.id for p in results})

    def test_food_budget_does_not_restrict_entertainment(self):
        results = filter_places(self.catalog.places, ExploreFilters(kind="fun", budget=50, budget_scope="food"))
        self.assertIn("demo-fun-2", {p.id for p in results})

    def test_area_and_diet_constraints(self):
        results = filter_places(self.catalog.places, ExploreFilters(area="春熙路", preferences=["不吃辣"]))
        self.assertEqual([p.id for p in results], ["demo-food-1"])

    def test_nearby_uses_actual_anchor_and_keeps_catalog_immutable(self):
        anchor = self.catalog.get("demo-food-2")
        filters = ExploreFilters(kind="fun", anchor_id=anchor.id, radius_m=800)
        results = filter_places(self.catalog.places, filters, anchor)
        self.assertIn("demo-fun-2", {p.id for p in results})
        self.assertNotIn("demo-fun-3", {p.id for p in results})
        self.assertTrue(all(p.distance_m <= 800 for p in results))
        self.assertTrue(all(p.distance_m is None for p in self.catalog.places))
        self.assertEqual(distance_m(anchor.location, anchor.location), 0)

    def test_other_city_never_gets_chengdu_demo_places(self):
        self.assertEqual(filter_places(self.catalog.places, ExploreFilters(city="杭州")), [])

    def test_invalid_input_is_rejected(self):
        with self.assertRaises(ValidationError):
            ExploreFilters(city=" ")
        with self.assertRaises(ValidationError):
            ExploreFilters(budget=-1)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.state = SessionState(page="food", filters=ExploreFilters(area="春熙路", budget=100, preferences=["不吃辣"]))

    def test_followup_keeps_budget_and_preferences(self):
        plan = rule_plan("换成玉林附近的", self.state)
        self.assertEqual(plan.filters.area, "玉林")
        self.assertEqual(plan.filters.budget, 100)
        self.assertIn("不吃辣", plan.filters.preferences)
        self.assertEqual(plan.agents, ["food"])

    def test_multi_domain_request_dispatches_both(self):
        plan = rule_plan("晚上吃点东西，再找地方玩", self.state)
        self.assertEqual(plan.agents, ["food", "fun"])
        self.assertEqual(plan.filters.budget_scope, "food")

    def test_followup_keeps_previous_specialists(self):
        self.state.last_agents = ["food", "fun"]
        plan = rule_plan("换成玉林附近的", self.state)
        self.assertEqual(plan.agents, ["food", "fun"])

    def test_nearby_entertainment_does_not_search_food_again(self):
        self.state.selected_place = DemoCatalog().get("demo-food-2")
        plan = rule_plan("吃完这家附近有什么好玩的", self.state)
        self.assertEqual(plan.agents, ["fun"])
        self.assertEqual(plan.filters.anchor_id, "demo-food-2")
        self.assertEqual(plan.filters.area, "")

    def test_city_change_clears_old_geographic_constraints(self):
        self.state.filters.anchor_id = "demo-food-2"
        plan = rule_plan("换成杭州，找点吃的", self.state)
        self.assertEqual(plan.filters.city, "杭州")
        self.assertEqual(plan.filters.area, "")
        self.assertIsNone(plan.filters.anchor_id)
        self.assertEqual(plan.filters.budget, 100)

    def test_budget_can_be_changed_and_removed(self):
        plan = rule_plan("换便宜一点", self.state)
        self.assertEqual(plan.filters.budget, 80)
        plan = rule_plan("人均五十元以内", self.state)
        self.assertEqual(plan.filters.budget, 50)
        plan = rule_plan("取消预算", self.state)
        self.assertIsNone(plan.filters.budget)


if __name__ == "__main__":
    unittest.main()
