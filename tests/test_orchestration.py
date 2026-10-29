import unittest
from collections import Counter

from urban_explorer.agents import AgentTeam
from urban_explorer.models import ExploreFilters, Place, SearchResult, SessionState
from test_agents import FakeProviders


class TaskProviders(FakeProviders):
    def __init__(self, always_wrong=False):
        super().__init__()
        self.calls = Counter()
        self.inputs = []
        self.always_wrong = always_wrong

    async def model_json(self, system, payload):
        if 'reference_plan' in payload:
            plan = payload['reference_plan']
            plan['agents'] = ['food', 'fun']
            plan['tasks'] = {
                'food': ExploreFilters(budget=80, preferences=['清淡']).model_dump(),
                'fun': ExploreFilters(kind='fun', category='书店', budget=30, budget_scope='fun', preferences=['安静']).model_dump(),
            }
            return plan
        if 'repair_task' in payload:
            # Attempts to relax constraints are not accepted as task updates.
            return {'query': '书店', 'budget': 999, 'city': '上海', 'category': ''}
        return await super().model_json(system, payload)

    async def search_places(self, filters, anchor=None, *, refresh=False):
        self.calls[filters.kind] += 1
        self.inputs.append((filters.model_copy(deep=True), refresh))
        wrong = filters.kind == 'fun' and (self.always_wrong or self.calls['fun'] == 1)
        place = Place(id='amap-' + filters.kind, name='测试公园' if wrong else '测试书店' if filters.kind == 'fun' else '测试餐厅',
            city=filters.city, kind=filters.kind, category='公园' if wrong else '书店' if filters.kind == 'fun' else '餐厅',
            cost=20 if filters.kind == 'fun' else 60, mode='live')
        return SearchResult(places=[place], mode='live')


class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_failed_domain_retries_with_locked_constraints(self):
        providers = TaskProviders()
        result = await AgentTeam(providers).run('推荐清淡餐厅和安静书店，餐饮预算80元，娱乐预算30元', SessionState())
        self.assertEqual(providers.calls, {'food': 1, 'fun': 2})
        tasks = result['plan'].tasks
        self.assertEqual(tasks['food'].preferences, ['清淡'])
        self.assertEqual(tasks['fun'].preferences, ['安静'])
        self.assertEqual([f.budget for f, _ in providers.inputs if f.kind == 'fun'], [30, 30])
        repaired_filter, refresh = providers.inputs[-1]
        self.assertTrue(refresh)
        self.assertEqual(repaired_filter.city, '成都')
        self.assertEqual(repaired_filter.category, '书店')
        fun = next(r for r in result['results'] if r.agent == 'fun')
        self.assertEqual(fun.attempts, 2)
        self.assertEqual(len(fun.places), 1)
        self.assertEqual(fun.places[0].category, '书店')
        self.assertEqual(len(result['results']), 2)
        self.assertEqual(next(r for r in result['results'] if r.agent == 'food').attempts, 1)

    async def test_failed_second_attempt_stops_and_keeps_successful_branch(self):
        providers = TaskProviders(always_wrong=True)
        result = await AgentTeam(providers).run('推荐餐厅和书店', SessionState())
        self.assertEqual(providers.calls, {'food': 1, 'fun': 2})
        fun = next(r for r in result['results'] if r.agent == 'fun')
        self.assertEqual(fun.places, [])
        self.assertTrue(any('未自动放宽' in n for n in fun.notices))
        self.assertTrue(next(r for r in result['results'] if r.agent == 'food').places)
        self.assertNotIn('测试公园', result['answer'])

    async def test_valid_first_results_do_not_trigger_repair(self):
        providers = TaskProviders()
        providers.calls['fun'] = 1
        result = await AgentTeam(providers).run('推荐餐厅和书店', SessionState())
        self.assertEqual(providers.calls, {'food': 1, 'fun': 2})
        self.assertTrue(all(not refresh for _, refresh in providers.inputs))
        self.assertTrue(all(r.attempts == 1 for r in result['results']))

    async def test_repair_cannot_return_over_budget_or_wrong_city(self):
        class UnsafeResult(TaskProviders):
            async def search_places(self, filters, anchor=None, *, refresh=False):
                result = await super().search_places(filters, anchor, refresh=refresh)
                if refresh:
                    result.places[0].cost = 31
                    result.places[0].city = '上海'
                return result
        providers = UnsafeResult()
        result = await AgentTeam(providers).run('推荐餐厅和书店', SessionState())
        self.assertEqual(providers.calls, {'food': 1, 'fun': 2})
        self.assertEqual(next(r for r in result['results'] if r.agent == 'fun').places, [])
