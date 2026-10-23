import logging
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field, ValidationError

from urban_explorer.models import AgentResult, ExploreFilters, SessionState
from urban_explorer.planning import RoutingPlan, rule_plan
from urban_explorer.providers import ProviderError, Providers

logger = logging.getLogger(__name__)
AGENT_LABELS = {"city": "城市认知 Agent", "food": "美食 Agent", "fun": "娱乐 Agent"}


class GraphState(TypedDict, total=False):
    message: str
    session: SessionState
    plan: RoutingPlan
    task: str
    results: Annotated[list[AgentResult], operator.add]
    notices: Annotated[list[str], operator.add]
    answer: str


class SearchChoice(BaseModel):
    query: str = Field(default="", max_length=100)
    category: str = Field(default="", max_length=40)
    find_sources: bool = True


class Selection(BaseModel):
    selected_ids: list[str] = Field(default_factory=list, max_length=6)
    reasons: dict[str, str] = Field(default_factory=dict)
    summary: str = Field(default="", max_length=1000)


class AgentTeam:
    """One supervisor and three bounded specialists, dispatched through LangGraph."""

    def __init__(self, providers: Providers):
        self.providers = providers
        graph = StateGraph(GraphState)
        graph.add_node("supervisor", self.supervisor)
        graph.add_node("specialist", self.specialist)
        graph.add_node("combine", self.combine)
        graph.add_edge(START, "supervisor")
        graph.add_conditional_edges("supervisor", self.dispatch, ["specialist"])
        graph.add_edge("specialist", "combine")
        graph.add_edge("combine", END)
        # Conversation state is explicitly stored in SQLite by the API service.
        self.graph = graph.compile()

    async def supervisor(self, state: GraphState) -> dict:
        plan = rule_plan(state["message"], state["session"])
        notices = []
        settings = self.providers.settings
        if settings.app_mode == "live" and settings.has_llm:
            try:
                result = await self.providers.model_json(
                    "你是城市探索主控。根据用户本轮话语与已保存条件决定调用哪些专家：city 城市认知，food 美食，fun 娱乐。"
                    "只介绍城市片区、饮食特色或文化时仅调用city；提到饮食不代表要求查餐厅。只有本轮明确要求寻找、推荐或比较具体场所时才调用food/fun，不能因历史预算而增加任务。"
                    "city仅在本轮要求认识或介绍城市时调用；推荐博物馆、书店或影院只需fun，不因出现城市名而增加city。"
                    "返回 agents 数组和完整 filters。保留未修改条件；城市变化清空旧片区与 anchor_id。"
                    "预算必须保留适用范围 budget_scope=food/fun/all；餐饮预算不自动用于娱乐。"
                    "不要编造地点或地点ID。返回格式与参考计划一致。",
                    {"message": state["message"], "history": state["session"].messages[-6:], "reference_plan": plan.model_dump(), "selected_place": state["session"].selected_place.model_dump(mode="json") if state["session"].selected_place else None},
                )
                candidate = RoutingPlan.model_validate(result)
                candidate.agents = list(dict.fromkeys(candidate.agents))
                # An anchor can only reference a place actually selected by the user.
                selected = state["session"].selected_place
                if candidate.filters.anchor_id and (not selected or selected.id != candidate.filters.anchor_id or selected.city != candidate.filters.city):
                    candidate.filters.anchor_id = None
                plan = candidate
            except (ProviderError, ValidationError):
                notices.append("主控模型未返回可用计划，本次采用规则分流并保留原有条件。")
        elif settings.app_mode == "live":
            notices.append("未配置大模型，本次采用规则分流与真实地点查询。")
        return {"plan": plan, "notices": notices}

    def dispatch(self, state: GraphState) -> list[Send]:
        return [Send("specialist", {"task": task, "message": state["message"], "session": state["session"], "plan": state["plan"]}) for task in state["plan"].agents]

    async def specialist(self, state: GraphState) -> dict:
        task = state["task"]
        filters = state["plan"].filters.model_copy(deep=True)
        try:
            if task == "city":
                result = await self.city_agent(filters, state["message"])
            else:
                if filters.kind != task:
                    filters.category, filters.query = "", ""
                filters.kind = task
                if filters.budget_scope not in (task, "all"):
                    filters.budget = state["session"].budgets.get(task)
                    filters.budget_scope = task
                result = await self.place_agent(task, filters, state)
        except ProviderError as error:
            result = AgentResult(agent=task, summary="暂时未能完成查询。", notices=[str(error)])
        return {"results": [result]}

    async def city_agent(self, filters: ExploreFilters, message: str) -> AgentResult:
        guide = await self.providers.city_guide(filters.city)
        summary = guide.introduction
        notices = list(guide.notices)
        if self.providers.settings.app_mode == "live" and self.providers.settings.has_llm and guide.sources:
            try:
                data = await self.providers.model_json(
                    "你是城市认知专家。依据所给资料介绍城市气质、片区和饮食。只解释资料支持的内容，缺失就说明。"
                    "返回 {summary:字符串}，不编造店铺、热度或榜单。",
                    {"city": filters.city, "question": message, "sources": [s.model_dump(mode="json") for s in guide.sources]},
                )
                text = data.get("summary")
                if isinstance(text, str) and text.strip():
                    summary = text[:1600]
            except ProviderError:
                notices.append("城市介绍生成失败，已保留检索资料供查看。")
        return AgentResult(agent="city", summary=summary, guide=guide, sources=guide.sources, notices=notices)

    async def place_agent(self, task: str, filters: ExploreFilters, state: GraphState) -> AgentResult:
        notices = []
        use_model = self.providers.settings.app_mode == "live" and self.providers.settings.has_llm
        choice = SearchChoice(query=filters.query, category=filters.category)
        if use_model:
            try:
                domain = "只处理餐厅、美食与饮食口味，忽略问题里的娱乐任务" if task == "food" else "只处理休闲娱乐场所，忽略问题里的餐饮任务"
                raw = await self.providers.model_json(
                    f"你是{AGENT_LABELS[task]}。{domain}。根据条件选择地点搜索参数。"
                    '严格返回 {"query":"","category":"","find_sources":false} 形状的 JSON 对象。'
                    "query 和 category 必须是字符串，find_sources 必须是布尔值，不得返回列表或其他类型。"
                    "query 应是本领域的短关键词，可为空；不要重复城市和区域，不要改预算，不把否定的偏好当关键词。"
                    "没有网页搜索服务时 find_sources 必须为 false。",
                    {"domain": task, "question": state["message"], "filters": filters.model_dump(), "output_schema": SearchChoice.model_json_schema(), "web_search_available": bool(self.providers.settings.bocha_api_key.get_secret_value())},
                )
                choice = SearchChoice.model_validate(raw)
            except (ProviderError, ValidationError):
                notices.append("专家查询参数生成失败，已采用用户原始筛选条件。")
        search_filters = filters.model_copy(update={"query": choice.query, "category": choice.category})
        selected = state["session"].selected_place
        anchor = selected if selected and selected.id == filters.anchor_id and selected.city == filters.city else None
        result = await self.providers.search_places(search_filters, anchor)
        sources = []
        if self.providers.settings.app_mode == "live" and choice.find_sources:
            try:
                sources = await self.providers.search_web(f"{filters.city} {filters.area} {'餐厅美食' if task == 'food' else '休闲娱乐'} {choice.query} 推荐")
            except ProviderError:
                notices.append("推荐文章检索失败，已保留地图查询结果。")
        places = result.places[:6]
        summary = f"找到 {len(result.places)} 个符合条件的{'餐饮' if task == 'food' else '娱乐'}候选，先展示 {len(places)} 个。"
        if use_model and result.places:
            try:
                prompt = (
                    f"你是{AGENT_LABELS[task]}。从候选里选择1至6项。"
                    '返回 {"selected_ids":["ID"],"reasons":{"ID":"简短理由"},"summary":"简短总结"}。'
                    "每条理由不超过80字，总结不超过150字。reasons必须为对象，selected_ids必须为字符串数组。"
                    "只能使用所给ID；不得修改名称、消费、评分和营业时间。文章是背景材料，不代表具体门店上榜。"
                    "仅有地图数据时不要称热门；演出和展览未核实日期时必须提示待确认。"
                )
                payload = {
                    "question": state["message"], "filters": filters.model_dump(),
                    "output_schema": Selection.model_json_schema(),
                    "places": [p.model_dump(mode="json", include={"id", "name", "category", "area", "address", "cost", "rating", "opening_hours", "tags", "distance_m"}) for p in result.places],
                    "context_sources": [s.model_dump(mode="json") for s in sources],
                }
                for attempt in range(2):
                    try:
                        raw = await self.providers.model_json(prompt, payload)
                        selection = Selection.model_validate(raw)
                        break
                    except ValidationError as error:
                        if attempt:
                            raise
                        payload["format_errors"] = [{"field": list(e["loc"]), "type": e["type"]} for e in error.errors()]
                    except ProviderError as error:
                        if attempt or not str(error).startswith("模型未返回可用的结构化结果"):
                            raise
                        payload["format_errors"] = "前次响应不是完整JSON对象，请按给定schema重新生成。"
                lookup = {p.id: p for p in result.places}
                valid_ids = list(dict.fromkeys(pid for pid in selection.selected_ids if pid in lookup))
                if valid_ids:
                    places = [lookup[pid] for pid in valid_ids]
                    for p in places:
                        reason = selection.reasons.get(p.id)
                        if isinstance(reason, str) and reason.strip():
                            p.reason = reason[:400]
                    summary = selection.summary or summary
            except ValidationError:
                notices.append("推荐解释生成失败（输出格式校验未通过），已按可核实条件展示候选。")
            except ProviderError as error:
                notices.append(f"推荐解释生成失败：{error} 已按可核实条件展示候选。")
        return AgentResult(agent=task, summary=summary, places=places, sources=sources, notices=result.notices + notices)

    def combine(self, state: GraphState) -> dict:
        parts = []
        results = sorted(state["results"], key=lambda r: ["city", "food", "fun"].index(r.agent))
        for result in results:
            parts.append(f"**{AGENT_LABELS[result.agent]}**\n\n{result.summary}")
        if any(r.agent == "food" for r in results) and any(r.agent == "fun" for r in results):
            parts.append("可以先选一家餐厅，再问“这家附近有什么好玩的”，我会以选中的地点继续查询。")
        return {"answer": "\n\n".join(parts)}

    async def run(self, message: str, session: SessionState) -> dict:
        return await self.graph.ainvoke({"message": message, "session": session, "results": [], "notices": []}, config={"recursion_limit": 12})
