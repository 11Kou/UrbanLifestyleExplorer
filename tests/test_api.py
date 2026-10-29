import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if any(importlib.util.find_spec(name) is None for name in ["fastapi", "langgraph", "sqlalchemy", "pydantic_settings", "httpx"]):
    raise unittest.SkipTest("完整依赖尚未安装，接口与多 Agent 集成测试暂不可运行")

from fastapi.testclient import TestClient

from urban_explorer.api import create_app
from urban_explorer.config import Settings


class ApiTests(unittest.TestCase):
    def test_independent_task_conditions_survive_restart(self):
        from test_orchestration import TaskProviders
        from urban_explorer.agents import AgentTeam
        with patch('urban_explorer.api.AgentTeam', return_value=AgentTeam(TaskProviders())):
            with TestClient(create_app(self.settings)) as client:
                reply = client.post('/chat', json={'session_id': self.sid, 'message': '推荐餐厅和书店'})
                self.assertEqual(reply.status_code, 200)
                self.assertEqual(reply.json()['session']['budgets'], {'food': 80, 'fun': 30})
        with TestClient(create_app(self.settings)) as restarted:
            state = restarted.get(f'/sessions/{self.sid}').json()
        self.assertEqual(state['domain_filters']['food']['preferences'], ['清淡'])
        self.assertEqual(state['domain_filters']['fun']['category'], '书店')
        self.assertEqual(state['domain_filters']['fun']['preferences'], ['安静'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(app_mode="demo", database_url="sqlite:///" + str(Path(self.temp.name) / "test.db"), amap_api_key="", bocha_api_key="", llm_api_key="", _env_file=None)
        self.client = TestClient(create_app(self.settings))
        self.client.__enter__()
        self.sid = "test_session_12345678"

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_favorites_are_idempotent_and_private_to_session(self):
        response = self.client.post(f"/sessions/{self.sid}/search", json={"city": "成都", "kind": "food", "budget": 50})
        self.assertEqual(response.status_code, 200)
        place = response.json()["places"][0]
        for _ in range(2):
            self.assertEqual(self.client.put(f"/sessions/{self.sid}/favorites/{place['id']}").status_code, 200)
        self.assertEqual(len(self.client.get(f"/sessions/{self.sid}/favorites").json()), 1)
        self.assertEqual(self.client.get("/sessions/other_session_123456/favorites").json(), [])
        self.client.delete(f"/sessions/{self.sid}/favorites/{place['id']}")
        self.assertEqual(self.client.get(f"/sessions/{self.sid}/favorites").json(), [])

    def test_multi_agent_followup_persists_filters(self):
        first = self.client.post("/chat", json={"session_id": self.sid, "message": "推荐人均100元以内的餐厅，再找地方玩", "context": {"city": "成都", "area": "春熙路"}, "page": "food"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["agents"], ["food", "fun"])
        second = self.client.post("/chat", json={"session_id": self.sid, "message": "换成玉林附近的"})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["session"]["filters"]["area"], "玉林")
        self.assertEqual(second.json()["session"]["filters"]["budget"], 100)

    def test_recommendation_evidence_survives_favorites_and_restart(self):
        response = self.client.post(f"/sessions/{self.sid}/search", json={
            "city": "成都", "kind": "food", "budget": 50, "preferences": ["安静"],
        })
        self.assertEqual(response.status_code, 200)
        place = response.json()["places"][0]
        self.assertTrue(place["match_reasons"])
        self.assertEqual(place["unverified_preferences"], ["安静"])
        self.assertEqual(self.client.put(f"/sessions/{self.sid}/favorites/{place['id']}").status_code, 200)
        with TestClient(create_app(self.settings)) as restarted:
            saved = restarted.get(f"/sessions/{self.sid}/favorites").json()[0]
            state = restarted.get(f"/sessions/{self.sid}").json()
        self.assertEqual(saved["match_reasons"], place["match_reasons"])
        self.assertEqual(saved["unverified_preferences"], ["安静"])
        self.assertEqual(state["results"][0]["match_reasons"], place["match_reasons"])

    def test_unknown_and_fabricated_places_cannot_be_saved(self):
        response = self.client.put(f"/sessions/{self.sid}/favorites/not-a-real-place")
        self.assertEqual(response.status_code, 404)

    def test_explicit_preference_change_is_not_overwritten_next_turn(self):
        self.client.put(f"/sessions/{self.sid}/preferences", json={"preferences": ["不吃辣"]})
        first = self.client.post("/chat", json={"session_id": self.sid, "message": "我现在可以吃辣，找餐厅", "page": "food"})
        self.assertEqual(first.status_code, 200)
        self.assertNotIn("不吃辣", first.json()["session"]["filters"]["preferences"])
        second = self.client.post("/chat", json={"session_id": self.sid, "message": "换成玉林附近的"})
        self.assertNotIn("不吃辣", second.json()["session"]["filters"]["preferences"])

    def test_conversation_survives_application_restart(self):
        self.client.post("/chat", json={"session_id": self.sid, "message": "玉林附近人均50元以内的美食", "page": "food"})
        with TestClient(create_app(self.settings)) as restarted:
            state = restarted.get(f"/sessions/{self.sid}").json()
        self.assertEqual(state["filters"]["area"], "玉林")
        self.assertEqual(state["filters"]["budget"], 50)
        self.assertEqual(len(state["messages"]), 2)

    def test_mixed_query_keeps_food_budget_after_browsing_entertainment(self):
        self.client.post(f"/sessions/{self.sid}/search", json={"city": "成都", "kind": "food", "budget": 50, "budget_scope": "food"})
        self.client.post(f"/sessions/{self.sid}/search", json={"city": "成都", "kind": "fun", "budget": None, "budget_scope": "fun"})
        response = self.client.post("/chat", json={"session_id": self.sid, "message": "吃点东西，再找地方玩", "page": "home"})
        self.assertEqual(response.status_code, 200)
        food = next(r for r in response.json()["results"] if r["agent"] == "food")
        self.assertTrue(food["places"])
        self.assertTrue(all(p["cost"] <= 50 for p in food["places"]))
        self.assertEqual(response.json()["session"]["budgets"]["food"], 50)

    def test_live_mode_never_silently_returns_demo_data(self):
        settings = Settings(app_mode="live", database_url=self.settings.database_url, amap_api_key="", bocha_api_key="", llm_api_key="", _env_file=None)
        with TestClient(create_app(settings)) as client:
            response = client.post(f"/sessions/{self.sid}/search", json={"city": "成都", "kind": "food"})
            self.assertEqual(response.status_code, 502)
            self.assertIn("AMAP_API_KEY", response.json()["detail"])

    def test_location_rejects_invalid_coordinates_and_demo_mode(self):
        self.assertEqual(self.client.post("/location/browser", json={"longitude": 181, "latitude": 30}).status_code, 422)
        self.assertEqual(self.client.post("/location/network").status_code, 502)


if __name__ == "__main__":
    unittest.main()
