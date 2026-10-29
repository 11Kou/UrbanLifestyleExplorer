import importlib.util
from types import SimpleNamespace
import unittest
from pydantic import SecretStr

if any(importlib.util.find_spec(name) is None for name in ["langgraph", "httpx", "pydantic_settings"]):
    raise unittest.SkipTest("完整依赖尚未安装，多 Agent 流程测试暂不可运行")

from urban_explorer.agents import AgentTeam
from urban_explorer.catalog import DemoCatalog
from urban_explorer.models import AgentResult, ExploreFilters, SearchResult, SessionState
from urban_explorer.providers import ProviderError


class FakeProviders:
    settings = SimpleNamespace(app_mode="live", has_llm=True, bocha_api_key=SecretStr(""))

    def __init__(self):
        self.catalog = DemoCatalog()

    async def model_json(self, system, payload):
        if "reference_plan" in payload:
            return payload["reference_plan"]
        if "expert_results" in payload:
            return {"summary": "结合整体需求推荐。", "selections": [
                {"agent": r["agent"], "id": p["id"], "reason": "主管依据候选事实推荐。"}
                for r in payload["expert_results"] for p in r["places"][:3]
            ]}
        if "places" in payload:
            return {"selected_ids": [payload["places"][0]["id"], "fabricated-id"], "reasons": {}, "summary": "依据候选条件筛选。"}
        return {"query": "", "category": "", "find_sources": True}

    async def search_places(self, filters, anchor=None, *, refresh=False):
        p = next(p for p in self.catalog.places if p.kind == filters.kind).model_copy(update={"mode": "live"})
        return SearchResult(places=[p], mode="live")

    async def search_web(self, query):
        return []


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_supervisor_overselecting_is_capped_without_reordering(self):
        class TooMany(FakeProviders):
            async def search_places(self, filters, anchor=None, *, refresh=False):
                return SearchResult(places=[p.model_copy(update={"mode": "live"})
                    for p in self.catalog.places if p.kind == "food"][:4], mode="live")

            async def model_json(self, system, payload):
                if "places" in payload:
                    return {"selected_ids": [p["id"] for p in payload["places"]]}
                if "expert_results" in payload:
                    return {"summary": "依据预算和位置选择。", "selections": [
                        {"agent": "food", "id": p["id"], "reason": "依据候选数据"}
                        for p in reversed(payload["expert_results"][0]["places"])
                    ]}
                return await super().model_json(system, payload)
        result = await AgentTeam(TooMany()).run("推荐餐厅", SessionState())
        self.assertEqual([p.id for p in result["results"][0].places], ["demo-food-1", "demo-food-2", "demo-food-3"])
        self.assertEqual(result["notices"], [])

    async def test_no_place_candidates_skip_supervisor_review(self):
        class NoCalls(FakeProviders):
            async def model_json(self, system, payload):
                raise AssertionError("unnecessary supervisor call")
        for experts in ([AgentResult(agent="city", summary="城市介绍")],
                        [AgentResult(agent="food", summary="无候选", notices=["没有找到符合条件的地点"])]):
            with self.subTest(agent=experts[0].agent):
                result = await AgentTeam(NoCalls()).combine({"results": experts})
                self.assertEqual(result["final_results"], experts)
                self.assertEqual(result["notices"], [])

    async def test_supervisor_selects_from_each_expert_once_and_keeps_reasons(self):
        class Reviewing(FakeProviders):
            reviews = 0

            async def search_places(self, filters, anchor=None, *, refresh=False):
                return SearchResult(places=[p.model_copy(update={"mode": "live"})
                    for p in self.catalog.places if p.kind == filters.kind][:2], mode="live")

            async def model_json(self, system, payload):
                if "places" in payload:
                    return {"selected_ids": [p["id"] for p in payload["places"]], "reasons": {}, "summary": "专家初选"}
                if "expert_results" in payload:
                    self.reviews += 1
                    self.review_payload = payload
                    return {"summary": "先吃饭，再休闲。", "selections": [
                        {"agent": r["agent"], "id": r["places"][-1]["id"], "reason": "符合该领域条件，建议优先考虑。"}
                        for r in payload["expert_results"]
                    ]}
                return await super().model_json(system, payload)

        providers = Reviewing()
        state = SessionState(filters=ExploreFilters(budget=50), budgets={"food": 100, "fun": None})
        result = await AgentTeam(providers).run("吃点东西，再找地方玩", state)
        self.assertEqual(providers.reviews, 1)
        self.assertEqual(providers.review_payload["budgets"], {"food": 50, "fun": None})
        self.assertEqual({r.agent for r in result["results"]}, {"food", "fun"})
        for expert in result["results"]:
            self.assertEqual(len(expert.places), 1)
            self.assertTrue(expert.places[0].supervisor_reason)
            self.assertNotEqual(expert.places[0].reason, expert.places[0].supervisor_reason)
            self.assertIn(expert.places[0].name, result["answer"])
        self.assertIn("主管建议", result["answer"])
        self.assertEqual(result["notices"], [])

    async def test_supervisor_invalid_output_preserves_all_expert_results(self):
        for defect in ("unknown", "wrong_domain", "missing_domain", "duplicate", "blank_reason", "blank_summary", "schema", "network"):
            with self.subTest(defect=defect):
                class InvalidReview(FakeProviders):
                    async def model_json(self, system, payload):
                        raw = await super().model_json(system, payload)
                        if "expert_results" not in payload:
                            return raw
                        if defect == "network":
                            raise ProviderError("外部服务请求失败")
                        if defect == "schema":
                            return {"summary": "错误输出", "selections": "bad"}
                        if defect == "unknown": raw["selections"][0]["id"] = "fake"
                        if defect == "wrong_domain": raw["selections"][0]["agent"] = "fun"
                        if defect == "missing_domain": raw["selections"].pop()
                        if defect == "duplicate": raw["selections"].append(raw["selections"][0])
                        if defect == "blank_reason": raw["selections"][0]["reason"] = " "
                        if defect == "blank_summary": raw["summary"] = " "
                        return raw
                result = await AgentTeam(InvalidReview()).run("吃点东西，再找地方玩", SessionState())
                self.assertEqual(len(result["results"]), 2)
                self.assertTrue(all(r.places for r in result["results"]))
                self.assertTrue(all(not p.supervisor_reason for r in result["results"] for p in r.places))
                self.assertTrue(any("主管推荐生成失败" in n for n in result["notices"]))
                self.assertNotIn("主管建议", result["answer"])

    async def test_supervisor_preserves_empty_domain_and_its_failure_notice(self):
        class PartialFailure(FakeProviders):
            async def search_places(self, filters, anchor=None, *, refresh=False):
                if filters.kind == "fun":
                    raise ProviderError("娱乐查询失败")
                return await super().search_places(filters, anchor)
        result = await AgentTeam(PartialFailure()).run("吃点东西，再找地方玩", SessionState())
        fun = next(r for r in result["results"] if r.agent == "fun")
        self.assertEqual(fun.places, [])
        self.assertIn("娱乐查询失败", fun.notices)
        self.assertTrue(next(r for r in result["results"] if r.agent == "food").places[0].supervisor_reason)

    async def test_model_selection_keeps_candidate_order_and_evidence(self):
        class ReversedSelection(FakeProviders):
            async def search_places(self, filters, anchor=None, *, refresh=False):
                self.places = [p.model_copy(update={"mode": "live", "match_reasons": ["地图评分 4/5"],
                    "unverified_preferences": ["安静"]}) for p in self.catalog.places if p.kind == "food"][:3]
                return SearchResult(places=self.places, mode="live")

            async def model_json(self, system, payload):
                if "places" in payload:
                    self.payload = payload
                    ids = [p["id"] for p in reversed(payload["places"])]
                    return {"selected_ids": ids + [ids[0], "fake"], "reasons": {ids[0]: "依据候选"}, "summary": "推荐"}
                return await super().model_json(system, payload)

        providers = ReversedSelection()
        result = await AgentTeam(providers).run("推荐餐厅", SessionState())
        places = result["results"][0].places
        self.assertEqual([p.id for p in places], [p.id for p in providers.places])
        self.assertEqual(providers.payload["places"][0]["unverified_preferences"], ["安静"])
        self.assertEqual(places[0].match_reasons, ["地图评分 4/5"])
        self.assertEqual(places[-1].reason, "依据候选")
        self.assertNotEqual(providers.places[-1].reason, "依据候选")

    async def test_empty_or_fabricated_selection_uses_visible_fallback(self):
        for ids in ([], ["fabricated"]):
            with self.subTest(ids=ids):
                class InvalidIds(FakeProviders):
                    async def model_json(self, system, payload):
                        if "places" in payload:
                            return {"selected_ids": ids, "summary": "不应展示的摘要"}
                        return await super().model_json(system, payload)
                result = await AgentTeam(InvalidIds()).run("推荐餐厅", SessionState())
                expert = result["results"][0]
                self.assertTrue(expert.places)
                self.assertTrue(any("未选择有效候选" in n for n in expert.notices))
                self.assertNotEqual(expert.summary, "不应展示的摘要")

    async def test_museum_recommendation_does_not_add_city_overview(self):
        result = await AgentTeam(FakeProviders()).run("推荐北京的博物馆", SessionState())
        self.assertEqual(result["plan"].agents, ["fun"])
        self.assertEqual([r.agent for r in result["results"]], ["fun"])

    async def test_invalid_selection_is_retried_once_before_fallback(self):
        class InvalidFirst(FakeProviders):
            calls = 0

            async def model_json(self, system, payload):
                if "places" in payload:
                    self.calls += 1
                    if self.calls == 1:
                        return {"selected_ids": [payload["places"][0]["id"]], "reasons": ["wrong shape"]}
                return await super().model_json(system, payload)

        providers = InvalidFirst()
        result = await AgentTeam(providers).run("推荐餐厅", SessionState())
        self.assertEqual(providers.calls, 2)
        self.assertTrue(result["results"][0].places)
        self.assertFalse(result["results"][0].notices)

    async def test_repeated_invalid_selection_and_auth_errors_still_report_failure(self):
        for error_kind, expected_calls in (("schema", 2), ("auth", 1)):
            with self.subTest(error_kind=error_kind):
                class BrokenSelection(FakeProviders):
                    calls = 0

                    async def model_json(self, system, payload):
                        if "places" in payload:
                            self.calls += 1
                            if error_kind == "auth":
                                raise ProviderError("外部服务请求失败（HTTP 403）")
                            return {"selected_ids": "wrong shape"}
                        return await super().model_json(system, payload)

                providers = BrokenSelection()
                result = await AgentTeam(providers).run("推荐餐厅", SessionState())
                self.assertEqual(providers.calls, expected_calls)
                self.assertTrue(result["results"][0].places)
                self.assertTrue(any("推荐解释生成失败" in n for n in result["results"][0].notices))

    async def test_parallel_specialists_merge_and_do_not_accept_fabricated_ids(self):
        team = AgentTeam(FakeProviders())
        state = SessionState(page="food", filters=ExploreFilters())
        result = await team.run("吃点东西，再找地方玩", state)
        self.assertEqual({r.agent for r in result["results"]}, {"food", "fun"})
        ids = [p.id for r in result["results"] for p in r.places]
        self.assertEqual(len(ids), 2)
        self.assertNotIn("fabricated-id", ids)
        self.assertIn("美食 Agent", result["answer"])
        self.assertIn("娱乐 Agent", result["answer"])


if __name__ == "__main__":
    unittest.main()
