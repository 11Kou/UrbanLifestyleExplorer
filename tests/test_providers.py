import importlib.util
import json
import unittest

if any(importlib.util.find_spec(name) is None for name in ["httpx", "pydantic_settings"]):
    raise unittest.SkipTest("完整依赖尚未安装，外部接口契约测试暂不可运行")

import httpx

from urban_explorer.config import Settings
from urban_explorer.models import Coordinates, ExploreFilters
from urban_explorer.providers import ProviderError, Providers


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def settings(self):
        return Settings(app_mode="live", amap_api_key="test-map-key", bocha_api_key="", llm_api_key="", _env_file=None)

    async def test_museums_exclude_general_exhibition_halls(self):
        captured = []
        def respond(request):
            captured.append(request)
            return httpx.Response(200, json={"status": "1", "pois": [
                {"id": "museum", "name": "测试博物馆", "type": "科教文化服务;博物馆", "typecode": "140100", "cityname": "北京市"},
                {"id": "exhibit", "name": "城市规划展览馆", "type": "科教文化服务;展览馆", "typecode": "140200", "cityname": "北京市"},
            ]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await Providers(self.settings(), client).search_places(ExploreFilters(city="北京", kind="fun", query="博物馆"))
        self.assertEqual(captured[0].url.params["types"], "140100")
        self.assertEqual([p.id for p in result.places], ["amap-museum"])

    async def test_bookstores_use_shopping_type_and_reject_reading_spaces(self):
        captured = []
        def respond(request):
            captured.append(request)
            return httpx.Response(200, json={"status": "1", "pois": [
                {"id": "bookstore", "name": "测试书店", "type": "购物服务;专卖店;书店", "typecode": "061205", "cityname": "成都市"},
                {"id": "reading", "name": "某某城市书房", "type": "体育休闲服务;休闲场所", "typecode": "080000", "cityname": "成都市"},
                {"id": "historic", "name": "某某书店旧址", "type": "风景名胜;旅游景点", "typecode": "110000", "cityname": "成都市"},
            ]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await Providers(self.settings(), client).search_places(ExploreFilters(
                kind="fun", category="书店", query="适合一个人去的安静书店", preferences=["安静", "一个人"],
            ))
        self.assertEqual(captured[0].url.params["types"], "061205")
        self.assertEqual(captured[0].url.params["keywords"], "书店")
        self.assertEqual([p.id for p in result.places], ["amap-bookstore"])

    async def test_place_introduction_and_photos_only_use_provider_facts(self):
        async with httpx.AsyncClient() as client:
            providers = Providers(self.settings(), client)
            raw = {"id": "intro", "name": "测试茶室", "adname": "青羊区", "type": "娱乐;茶艺馆", "address": "测试路1号",
                   "business": {"tag": "盖碗茶, 点心", "cost": "35"},
                   "photos": [{"url": "https://example.com/place.jpg", "title": "门面"}, {"url": "not-a-url"}, None]}
            place = providers.parse_place(raw, "成都", "fun")
            self.assertIn("位于青羊区的茶艺馆", place.reason)
            self.assertIn("盖碗茶、点心", place.reason)
            self.assertIn("35 元", place.reason)
            self.assertNotIn("安静", place.reason)
            self.assertEqual(len(place.photos), 1)
            self.assertEqual(place.photos[0].title, "门面")
            raw.update(business={}, photos={})
            sparse = providers.parse_place(raw, "成都", "fun")
            self.assertIn("测试路1号", sparse.reason)
            self.assertNotIn("35", sparse.reason)
            self.assertEqual(sparse.photos, [])

    async def test_network_location_returns_city_not_precise_coordinates(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "status": "1", "city": "杭州市", "province": "浙江省", "rectangle": "ignored",
        }))) as client:
            result = await Providers(self.settings(), client).locate_city()
        self.assertEqual(result.city, "杭州")
        self.assertEqual(result.source, "ip")
        self.assertNotIn("location", result.model_dump())

    async def test_browser_location_converts_gps_and_handles_municipality(self):
        calls = []
        def respond(request):
            calls.append(request)
            if request.url.path.endswith("convert"):
                self.assertEqual(request.url.params["coordsys"], "gps")
                return httpx.Response(200, json={"status": "1", "locations": "116.406,39.901"})
            self.assertEqual(request.url.params["location"], "116.406,39.901")
            return httpx.Response(200, json={"status": "1", "regeocode": {"addressComponent": {
                "city": [], "province": "北京市", "district": "东城区",
            }}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await Providers(self.settings(), client).locate_city(Coordinates(longitude=116.4, latitude=39.9))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.city, "北京")
        self.assertEqual(result.district, "东城区")
        self.assertEqual(result.source, "browser")

    async def test_unknown_network_location_never_defaults_to_chengdu(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "status": "1", "city": [], "province": [],
        }))) as client:
            with self.assertRaises(ProviderError):
                await Providers(self.settings(), client).locate_city()

    async def test_denied_location_service_has_safe_error(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "status": "0", "info": "test-map-key",
        }))) as client:
            with self.assertRaises(ProviderError) as error:
                await Providers(self.settings(), client).locate_city()
            self.assertNotIn("test-map-key", str(error.exception))

    async def test_live_chengdu_guide_without_search_has_official_sources(self):
        def unexpected(request):
            raise AssertionError("No web request expected without a search key")
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            providers = Providers(self.settings(), client)
            guide = await providers.city_guide("成都")
            self.assertEqual(guide.mode, "live")
            self.assertEqual([a.name for a in guide.areas], ["春熙路", "玉林", "少城"])
            self.assertTrue(all(s.kind == "guide" and s.url for s in guide.sources))
            guide.areas.clear()
            self.assertEqual(len((await providers.city_guide("成都")).areas), 3)
            other = await providers.city_guide("杭州")
            self.assertEqual(other.areas, [])
            self.assertNotIn("成都", other.introduction)

    async def test_optional_search_failure_preserves_curated_guide(self):
        settings = self.settings().model_copy(update={"bocha_api_key": self.settings().amap_api_key})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403))) as client:
            providers = Providers(settings, client)
            self.assertTrue((await providers.city_guide("成都")).sources)
            guide = await providers.city_guide("杭州")
            self.assertTrue(guide.notices)
            self.assertEqual(guide.sources, [])

    async def test_city_auto_search_generates_grounded_guide_and_caches_it(self):
        calls = []
        def respond(request):
            calls.append(request)
            if request.url.path.endswith("search"):
                return httpx.Response(200, json={"results": [
                    {"title": "杭州城市概况", "url": "https://www.hangzhou.gov.cn/intro", "summary": "杭州有湖滨街区和片儿川。"},
                    {"title": "广告", "url": "https://example.com/ad", "summary": "不可信商家广告"},
                ]})
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                "introduction": "杭州可从湖滨街区开始探索。", "tags": ["湖滨"],
                "areas": [{"name": "湖滨", "description": "资料介绍的街区", "tags": []}],
                "foods": ["片儿川"], "source_ids": [0], "city": "伪造城市", "sources": [],
            }, ensure_ascii=False)}}]})
        settings = Settings(app_mode="live", bocha_api_key="", llm_api_key="test-only", llm_base_url="https://api.deepseek.com", _env_file=None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            providers = Providers(settings, client)
            guide = await providers.city_guide("杭州")
            self.assertEqual(guide.city, "杭州")
            self.assertEqual(guide.foods, ["片儿川"])
            self.assertEqual(len(guide.sources), 1)
            self.assertEqual(guide.sources[0].url.host, "www.hangzhou.gov.cn")
            guide.areas.clear()
            self.assertEqual(len((await providers.city_guide("杭州")).areas), 1)
        self.assertEqual(len(calls), 2)

    async def test_city_generation_rejects_fabricated_citations(self):
        def respond(request):
            if request.url.path.endswith("search"):
                return httpx.Response(200, json={"results": [{"title": "杭州资料", "url": "https://baike.baidu.com/item/test", "summary": "杭州城市介绍"}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"introduction": "无依据生成内容", "source_ids": [99]})}}]})
        settings = Settings(app_mode="live", bocha_api_key="", llm_api_key="test-only", llm_base_url="https://api.deepseek.com", _env_file=None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            guide = await Providers(settings, client).city_guide("杭州")
        self.assertNotEqual(guide.introduction, "无依据生成内容")
        self.assertTrue(guide.notices)
        self.assertEqual(len(guide.sources), 1)

    async def test_provider_fields_remain_factual_and_unknown_cost_is_excluded(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={"status": "1", "pois": [
                {"id": "one", "name": "测试分店一", "cityname": "成都市", "adname": "锦江区", "address": "春熙路测试地址", "type": "餐饮服务;地方菜", "location": "104.08,30.65", "business": {"cost": "48", "rating": "4.5", "opentime_today": "10:00-20:00"}},
                {"id": "two", "name": "未知消费分店", "cityname": "成都市", "adname": "锦江区", "address": "春熙路", "type": "餐饮服务", "location": "104.08,30.65", "business": {"cost": []}},
            ]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            providers = Providers(self.settings(), client)
            filters = ExploreFilters(area="春熙路", budget=50)
            result = await providers.search_places(filters)
            second = await providers.search_places(filters)
        self.assertEqual(len(calls), 1, "Identical queries should reuse the short-term cache")
        self.assertEqual(calls[0].url.params["types"], "050000")
        self.assertEqual([p.id for p in result.places], ["amap-one"])
        place = second.places[0]
        self.assertEqual(place.cost, 48)
        self.assertEqual(place.opening_hours, "10:00-20:00")
        self.assertEqual(place.mode, "live")
        self.assertNotIn("test-map-key", str(place.sources[0].url))

    async def test_keywords_cannot_remove_food_category_restriction(self):
        captured = []
        def respond(request):
            captured.append(request)
            return httpx.Response(200, json={"status": "1", "pois": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await Providers(self.settings(), client).search_places(ExploreFilters(query="川菜"))
        self.assertEqual(captured[0].url.params["types"], "050000")

    async def test_auth_error_does_not_expose_secret_or_fallback_to_demo(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403, json={}))) as client:
            providers = Providers(self.settings(), client)
            with self.assertRaises(ProviderError) as error:
                await providers.search_places(ExploreFilters())
            self.assertNotIn("test-map-key", str(error.exception))

    async def test_deepseek_json_requests_are_bounded_and_non_thinking(self):
        captured = []
        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
        settings = Settings(app_mode="live", llm_api_key="test-only-key", llm_base_url="https://api.deepseek.com", llm_model="deepseek-flash", _env_file=None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await Providers(settings, client).model_json("返回确认结果", {"test": True})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured[0]["thinking"], {"type": "disabled"})
        self.assertEqual(captured[0]["max_tokens"], 2048)
        self.assertEqual(captured[0]["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
