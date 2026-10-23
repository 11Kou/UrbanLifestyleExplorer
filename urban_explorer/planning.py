import re
from typing import Literal

from pydantic import BaseModel, Field

from urban_explorer.models import ExploreFilters, SessionState


AgentName = Literal["city", "food", "fun"]


class RoutingPlan(BaseModel):
    agents: list[AgentName] = Field(min_length=1, max_length=3)
    filters: ExploreFilters


def rule_plan(message: str, state: SessionState) -> RoutingPlan:
    filters = state.filters.model_copy(deep=True)
    for city in ["成都", "杭州", "上海", "北京", "广州", "深圳", "重庆", "西安"]:
        if city in message:
            if city != filters.city:
                filters.area, filters.category, filters.query, filters.anchor_id = "", "", "", None
            filters.city = city
            break
    for area in ["春熙路", "玉林", "少城"]:
        if area in message:
            filters.area = area
            filters.anchor_id = None
    if any(word in message for word in ["不限片区", "不限区域", "全城", "换个片区"]):
        filters.area, filters.anchor_id = "", None

    has_food = any(word in message for word in ["吃", "餐", "美食", "小吃", "火锅", "面食", "轻食"])
    has_fun = any(word in message for word in ["玩", "娱乐", "展览", "看展", "音乐", "书店", "博物馆", "电影院", "影院", "茶室", "散步", "晚上"])
    # "吃完这家附近玩什么" references a chosen restaurant, rather than asking for another one.
    if state.selected_place and any(word in message for word in ["吃完", "附近", "周边"]):
        filters.anchor_id = state.selected_place.id
        if has_fun:
            has_food = False
            filters.area = ""
    agents: list[AgentName] = []
    if any(word in message for word in ["认识", "介绍", "特色", "片区", "了解城市"]):
        agents.append("city")
    if has_food:
        agents.append("food")
    if has_fun:
        agents.append("fun")
    if not agents:
        agents = state.last_agents or [filters.kind if state.page in ("food", "fun", "saved") else "city"]
    if len([a for a in agents if a in ("food", "fun")]) == 1:
        new_kind = "food" if "food" in agents else "fun"
        if filters.kind != new_kind:
            filters.category, filters.query = "", ""
        filters.kind = new_kind

    translated = message.replace("一百", "100").replace("五十", "50").replace("两百", "200").replace("二百", "200")
    amount = re.search(r"(?:人均|预算|每人|不超过|最多)\s*[¥￥]?\s*(\d+(?:\.\d+)?)", translated)
    if not amount:
        amount = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块)?\s*(?:以内|以下)", translated)
    if amount:
        budget = float(amount.group(1))
        if 0 < budget <= 100000:
            filters.budget = budget
            filters.budget_scope = "food" if has_food else "fun" if has_fun else filters.kind
    if any(word in message for word in ["预算不限", "不限预算", "取消预算"]):
        filters.budget = None
    if any(word in message for word in ["便宜一点", "便宜点"]):
        costs = [p.cost for p in state.results if p.kind == filters.kind and p.cost is not None]
        baseline = filters.budget or (min(costs) if costs else 100)
        filters.budget = max(1, round(baseline * 0.8))
        filters.budget_scope = filters.kind

    for pref in ["不吃辣", "清淡", "安静", "一个人", "朋友", "室内", "户外", "晚上"]:
        if pref in message and pref not in filters.preferences:
            filters.preferences.append(pref)
    if "可以吃辣" in message:
        filters.preferences = [p for p in filters.preferences if p != "不吃辣"]
    categories = {"火锅": "火锅", "小吃": "小吃", "面食": "面食", "轻食": "轻食", "地方菜": "地方菜", "音乐": "音乐", "看展": "文化", "散步": "街区漫步", "喝茶": "休闲"}
    for word, category in categories.items():
        if word in message:
            filters.category = category
    if any(word in message for word in ["不限类别", "换个类型", "取消类别"]):
        filters.category, filters.query = "", ""
    return RoutingPlan(agents=list(dict.fromkeys(agents)), filters=filters)
