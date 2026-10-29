import logging
import asyncio
import operator
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field, ValidationError

from urban_explorer.models import AgentResult, ExploreFilters, SessionState
from urban_explorer.planning import RoutingPlan, rule_plan, task_filters
from urban_explorer.recommendations import filter_places
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
    final_results: list[AgentResult]
    audited_results: list[AgentResult]
    repair_tasks: dict[str, str]
    retry_query: str
    retry: bool


class SearchChoice(BaseModel):
    query: str = Field(default="", max_length=100)
    category: str = Field(default="", max_length=40)
    find_sources: bool = True


class Selection(BaseModel):
    selected_ids: list[str] = Field(default_factory=list, max_length=6)
    reasons: dict[str, str] = Field(default_factory=dict)
    summary: str = Field(default="", max_length=1000)


class ReviewedPlace(BaseModel):
    agent: Literal["food", "fun"]
    id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=400)


class FinalReview(BaseModel):
    summary: str = Field(min_length=1, max_length=1000)
    selections: list[ReviewedPlace] = Field(default_factory=list, max_length=6)


class AgentTeam:
    """One supervisor and three bounded specialists, dispatched through LangGraph."""

    def __init__(self, providers: Providers):
        self.providers = providers
        graph = StateGraph(GraphState)
        graph.add_node("supervisor", self.supervisor)
        graph.add_node("specialist", self.specialist)
        graph.add_node("combine", self.combine)
        graph.add_node("audit", self.audit)
        graph.add_node("repair", self.repair)
        graph.add_edge(START, "supervisor")
        graph.add_conditional_edges("supervisor", self.dispatch, ["specialist"])
        graph.add_edge("specialist", "audit")
        graph.add_conditional_edges("audit", lambda state: "repair" if state["repair_tasks"] else "combine", ["repair", "combine"])
        graph.add_edge("repair", "combine")
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
                    "同时返回tasks对象，key为每个已选专家，value为各自完整ExploreFilters；各领域预算、类别、偏好独立。"
                    "例如餐厅清淡预算80，书店安静预算30，应分别放入food和fun任务；保留各领域未修改的历史条件。"
                    "tasks与agents必须一致；不要编造地点或地点ID。",
                    {"message": state["message"], "history": state["session"].messages[-6:], "reference_plan": plan.model_copy(update={"tasks": task_filters(plan, state["session"])}).model_dump(), "domain_history": {k: v.model_dump() for k, v in state["session"].domain_filters.items()}, "output_schema": RoutingPlan.model_json_schema(), "selected_place": state["session"].selected_place.model_dump(mode="json") if state["session"].selected_place else None},
                )
                candidate = RoutingPlan.model_validate(result)
                candidate.agents = list(dict.fromkeys(candidate.agents))
                if candidate.tasks and set(candidate.tasks) != set(candidate.agents):
                    raise ValueError("task domains do not match routing")
                # An anchor can only reference a place actually selected by the user.
                selected = state["session"].selected_place
                if candidate.filters.anchor_id and (not selected or selected.id != candidate.filters.anchor_id or selected.city != candidate.filters.city):
                    candidate.filters.anchor_id = None
                plan = candidate
            except (ProviderError, ValidationError, ValueError):
                notices.append("主控模型未返回可用计划，本次采用规则分流并保留原有条件。")
        elif settings.app_mode == "live":
            notices.append("未配置大模型，本次采用规则分流与真实地点查询。")
        plan.tasks = task_filters(plan, state["session"])
        return {"plan": plan, "notices": notices}

    def dispatch(self, state: GraphState) -> list[Send]:
        return [Send("specialist", {"task": task, "message": state["message"], "session": state["session"], "plan": state["plan"]}) for task in state["plan"].agents]

    async def specialist(self, state: GraphState) -> dict:
        task = state["task"]
        filters = state["plan"].tasks[task].model_copy(deep=True)
        try:
            if task == "city":
                result = await self.city_agent(filters, state["message"])
            else:
                result = await self.place_agent(task, filters, state)
        except ProviderError as error:
            result = AgentResult(agent=task, summary="暂时未能完成查询。", notices=[str(error)])
        return {"results": [result]}

    def validate_expert(self, result: AgentResult, state: GraphState) -> tuple[AgentResult, str]:
        result = result.model_copy(deep=True)
        if result.agent == "city":
            return result, ""
        filters = state["plan"].tasks[result.agent]
        anchor = state["session"].selected_place
        if not anchor or anchor.id != filters.anchor_id or anchor.city != filters.city:
            anchor = None
        original_count = len(result.places)
        # The tool handles keyword semantics; audit exact hard constraints again.
        allowed = {p.id for p in filter_places(result.places, filters.model_copy(update={"category": "", "query": ""}), anchor)}
        result.places = [p for p in result.places if p.id in allowed]
        category = filters.category
        terms = {"书店": ("书店", "书屋", "书城", "图书"), "博物馆": ("博物馆",), "电影院": ("电影院", "影院", "影城")}.get(category)
        if category:
            result.places = [p for p in result.places if any(t in p.name + p.category + " ".join(p.tags) for t in (terms or (category,)))]
        if not result.places:
            result.summary = "暂未找到满足本次任务约束的候选。"
            return result, "候选不满足城市、预算、片区或类别约束" if original_count else "查询未获得候选"
        if len(result.places) != original_count:
            result.summary = f"审核后保留 {len(result.places)} 个符合约束的候选。"
        return result, ""

    async def audit(self, state: GraphState) -> dict:
        results, repairs = [], {}
        for expert in state["results"]:
            checked, issue = self.validate_expert(expert, state)
            results.append(checked)
            if issue:
                repairs[expert.agent] = issue
        return {"audited_results": results, "repair_tasks": repairs}

    async def repair(self, state: GraphState) -> dict:
        async def retry(agent, issue):
            filters = state["plan"].tasks[agent]
            query = filters.category or filters.query
            if self.providers.settings.app_mode == "live" and self.providers.settings.has_llm:
                try:
                    raw = await self.providers.model_json(
                        "你是主管，为失败专家生成一次定向补查关键词。只返回 {query:字符串}。"
                        "仅可改写搜索关键词，不能扩大城市、片区、类别、预算或清除偏好；有明确类别时以类别为主要关键词。",
                        {"repair_task": agent, "issue": issue, "question": state["message"], "locked_filters": filters.model_dump()},
                    )
                    query = SearchChoice.model_validate(raw).query
                except (ProviderError, ValidationError):
                    pass
            if filters.query:
                query = filters.query
            update = await self.specialist({**state, "task": agent, "retry": True, "retry_query": query})
            checked, remaining = self.validate_expert(update["results"][0], state)
            checked.attempts = 2
            checked.notices.insert(0, f"已定向补查一次：{issue}。")
            if remaining:
                checked.notices.append("补查后仍没有符合条件的候选，未自动放宽限制；请确认是否调整类别、片区或预算。")
                checked.summary = "补查后仍无合适候选，请确认是否调整条件。"
            return checked
        repaired = await asyncio.gather(*(retry(agent, issue) for agent, issue in state["repair_tasks"].items()))
        replacements = {r.agent: r for r in repaired}
        return {"audited_results": [replacements.get(r.agent, r) for r in state["audited_results"]]}

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
        if use_model and not state.get("retry"):
            try:
                domain = "只处理餐厅、美食与饮食口味，忽略问题里的娱乐任务" if task == "food" else "只处理休闲娱乐场所，忽略问题里的餐饮任务"
                raw = await self.providers.model_json(
                    f"你是{AGENT_LABELS[task]}。{domain}。根据条件选择地点搜索参数。"
                    '严格返回 {"query":"","category":"","find_sources":false} 形状的 JSON 对象。'
                    "query 和 category 必须是字符串，find_sources 必须是布尔值，不得返回列表或其他类型。"
                    "query 应是本领域的短关键词，可为空；不要重复城市和区域，不要改预算，不把否定的偏好当关键词。"
                    "必须保留用户明确要求的场所类别，例如用户找书店，query填书店；娱乐不限预算只表示预算不限，不能把书店改成泛娱乐。"
                    "没有网页搜索服务时 find_sources 必须为 false。",
                    {"domain": task, "question": state["message"], "filters": filters.model_dump(), "output_schema": SearchChoice.model_json_schema(), "web_search_available": bool(self.providers.settings.bocha_api_key.get_secret_value())},
                )
                choice = SearchChoice.model_validate(raw)
            except (ProviderError, ValidationError):
                notices.append("专家查询参数生成失败，已采用用户原始筛选条件。")
        search_filters = filters.model_copy(update={"query": state.get("retry_query", filters.query or choice.query), "category": filters.category or choice.category})
        selected = state["session"].selected_place
        anchor = selected if selected and selected.id == filters.anchor_id and selected.city == filters.city else None
        result = await self.providers.search_places(search_filters, anchor, refresh=True) if state.get("retry") else await self.providers.search_places(search_filters, anchor)
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
                    "候选已按用户偏好、距离、地图评分和消费排序，最终展示将保留候选原有顺序。"
                    "推荐理由只能引用match_reasons或候选事实；unverified_preferences中的偏好必须说明待确认，不能声称已经满足。"
                    "仅有地图数据时不要称热门；演出和展览未核实日期时必须提示待确认。"
                )
                payload = {
                    "question": state["message"], "filters": filters.model_dump(),
                    "output_schema": Selection.model_json_schema(),
                    "places": [p.model_dump(mode="json", include={"id", "name", "category", "area", "address", "cost", "rating", "opening_hours", "tags", "distance_m", "match_reasons", "unverified_preferences"}) for p in result.places],
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
                    places = [p.model_copy(deep=True) for p in result.places if p.id in valid_ids]
                    for p in places:
                        reason = selection.reasons.get(p.id)
                        if isinstance(reason, str) and reason.strip():
                            p.reason = reason[:400]
                    summary = selection.summary or summary
                else:
                    notices.append("推荐解释生成失败（未选择有效候选），已按筛选排序展示候选。")
            except ValidationError:
                notices.append("推荐解释生成失败（输出格式校验未通过），已按可核实条件展示候选。")
            except ProviderError as error:
                notices.append(f"推荐解释生成失败：{error} 已按可核实条件展示候选。")
        return AgentResult(agent=task, summary=summary, places=places, sources=sources, notices=result.notices + notices)

    async def combine(self, state: GraphState) -> dict:
        parts = []
        results = sorted(state.get("audited_results", state["results"]), key=lambda r: ["city", "food", "fun"].index(r.agent))
        notices = []
        reviewed = False
        if self.providers.settings.app_mode == "live" and self.providers.settings.has_llm and any(r.places for r in results):
            try:
                filters = state["plan"].filters
                budgets = dict(state["session"].budgets)
                for domain in ("food", "fun"):
                    if domain in state["plan"].tasks:
                        budgets[domain] = state["plan"].tasks[domain].budget
                    elif filters.budget_scope in (domain, "all"):
                        budgets[domain] = filters.budget
                raw = await self.providers.model_json(
                    "你是城市探索主管，负责审核专家候选，结合用户整体要求给出最终推荐与理由。"
                    "每个有候选的领域必须保留1至3项；只选对应专家提供的地点ID，不得跨领域借用或编造ID。"
                    "返回summary整体建议和selections数组，每项含agent、id、reason；每条reason不超过80字，summary不超过200字。"
                    "summary只概述整体取舍，不逐一列店名；具体地点及理由放在selections中。"
                    "面向用户用自然语言表达，不出现food、fun等内部字段或执行过程术语。"
                    "理由需结合用户条件与match_reasons及地点事实，解释为什么选择；专家意见仅供参考。"
                    "unverified_preferences中的偏好必须说明待确认，不能声称已经满足。不得修改城市、预算、地点事实。"
                    "不虚构热度、用户评论、交通耗时、场次或店间距离；不得把各领域人均预算当作行程总预算。"
                    "候选已有规则排序，入选地点展示保留相对顺序；summary不使用第一名、第二名等位置引用。"
                    "某领域无候选或查询失败时如实说明，不丢弃失败提示，也不补造地点。",
                    {
                        "question": state["message"], "filters": state["plan"].filters.model_dump(),
                        "budgets": budgets,
                        "tasks": {k: v.model_dump() for k, v in state["plan"].tasks.items()},
                        "expert_results": [r.model_dump(mode="json", include={
                            "agent": True, "summary": True, "notices": True, "sources": True,
                            "places": {"__all__": {"id", "city", "name", "category", "area", "address", "cost", "rating", "opening_hours",
                                "tags", "distance_m", "reason", "match_reasons", "unverified_preferences"}},
                        }) for r in results],
                        "output_schema": FinalReview.model_json_schema(),
                    },
                )
                review = FinalReview.model_validate(raw)
                candidates = {(r.agent, p.id): p for r in results for p in r.places}
                choices = {(p.agent, p.id): p.reason.strip() for p in review.selections}
                expected = {r.agent for r in results if r.places}
                if (not review.summary.strip() or len(choices) != len(review.selections)
                        or any(key not in candidates or not reason for key, reason in choices.items())
                        or {agent for agent, _ in choices} != expected):
                    raise ValueError("invalid supervisor selection")
                final_results = [r.model_copy(deep=True) for r in results]
                for result in final_results:
                    result.places = [p for p in result.places if (result.agent, p.id) in choices][:3]
                    for place in result.places:
                        place.supervisor_reason = choices[(result.agent, place.id)]
                    if result.places:
                        result.summary = f"主管结合本次需求，从专家候选中推荐以下 {len(result.places)} 项。"
                results = final_results
                parts.append("**主管建议**\n\n" + review.summary.strip())
                reviewed = True
            except (ProviderError, ValidationError, ValueError):
                notices.append("主管推荐生成失败，已保留专家候选、推荐理由和筛选依据。")
        for result in results:
            parts.append(f"**{AGENT_LABELS[result.agent]}**\n\n{result.summary}")
            for place in result.places:
                reason = place.supervisor_reason if reviewed else place.reason
                parts.append(f"- **{place.name}**：{reason or '请结合下方筛选依据选择。'}")
                if place.unverified_preferences:
                    parts.append("  待确认偏好：" + "、".join(place.unverified_preferences))
        if any(r.agent == "food" for r in results) and any(r.agent == "fun" for r in results):
            parts.append("可以先选一家餐厅，再问“这家附近有什么好玩的”，我会以选中的地点继续查询。")
        return {"answer": "\n\n".join(parts), "final_results": results, "notices": notices}

    async def run(self, message: str, session: SessionState) -> dict:
        output = await self.graph.ainvoke({"message": message, "session": session, "results": [], "notices": []}, config={"recursion_limit": 12})
        # Publish reviewed results without appending them to the parallel reducer.
        output["results"] = output.pop("final_results")
        return output
