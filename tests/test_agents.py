import importlib.util
from types import SimpleNamespace
import unittest
from pydantic import SecretStr

if any(importlib.util.find_spec(name) is None for name in ["langgraph", "httpx", "pydantic_settings"]):
    raise unittest.SkipTest("完整依赖尚未安装，多 Agent 流程测试暂不可运行")

from urban_explorer.agents import AgentTeam
from urban_explorer.catalog import DemoCatalog
from urban_explorer.models import ExploreFilters, SearchResult, SessionState
from urban_explorer.providers import ProviderError


class FakeProviders:
    settings = SimpleNamespace(app_mode="live", has_llm=True, bocha_api_key=SecretStr(""))

    def __init__(self):
        self.catalog = DemoCatalog()

    async def model_json(self, system, payload):
        if "reference_plan" in payload:
            return payload["reference_plan"]
        if "places" in payload:
            return {"selected_ids": [payload["places"][0]["id"], "fabricated-id"], "reasons": {}, "summary": "依据候选条件筛选。"}
        return {"query": "", "category": "", "find_sources": True}

    async def search_places(self, filters, anchor=None):
        p = next(p for p in self.catalog.places if p.kind == filters.kind).model_copy(update={"mode": "live"})
        return SearchResult(places=[p], mode="live")

    async def search_web(self, query):
        return []


class AgentTests(unittest.IsolatedAsyncioTestCase):
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
