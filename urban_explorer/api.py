import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, Path, Request

from urban_explorer.agents import AgentTeam
from urban_explorer.config import Settings, get_settings
from urban_explorer.models import ChatRequest, ChatResponse, CityGuide, Coordinates, ExploreFilters, LocatedCity, Place, PreferenceUpdate, SearchResult, SessionState
from urban_explorer.providers import ProviderError, Providers
from urban_explorer.storage import Store

logger = logging.getLogger(__name__)
SessionID = Annotated[str, Path(min_length=16, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")]


class Services:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.store = Store(settings.database_url)
        self.providers = Providers(settings, client)
        self.agents = AgentTeam(self.providers)
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, session_id: str) -> asyncio.Lock:
        return self.locks.setdefault(session_id, asyncio.Lock())

    def find_place(self, session_id: str, place_id: str) -> Place | None:
        state = self.store.get_session(session_id)
        pool = state.results + self.store.favorites(session_id)
        if state.selected_place:
            pool.append(state.selected_place)
        return next((p for p in pool if p.id == place_id and p.mode == self.settings.app_mode), None)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # HTTP client logging includes query strings; keep credentials out of logs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        async with httpx.AsyncClient(timeout=settings.request_timeout, follow_redirects=False) as client:
            app.state.services = Services(settings, client)
            try:
                yield
            finally:
                app.state.services.store.engine.dispose()

    app = FastAPI(title="城市初见 API", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, error: ProviderError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=502, content={"detail": str(error)})

    def services() -> Services:
        return app.state.services

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": settings.app_mode, "capabilities": {
            "maps": bool(settings.amap_api_key.get_secret_value()),
            "web_search": bool(settings.bocha_api_key.get_secret_value()),
            "city_search": settings.has_city_search,
            "llm": settings.has_llm,
        }}

    @app.get("/cities/{city}", response_model=CityGuide)
    async def city_guide(city: str):
        try:
            city = ExploreFilters(city=city).city
        except ValueError:
            raise HTTPException(422, "请输入有效的城市名称。") from None
        return await services().providers.city_guide(city)

    @app.post("/location/browser", response_model=LocatedCity)
    async def browser_location(coordinates: Coordinates):
        return await services().providers.locate_city(coordinates)

    @app.post("/location/network", response_model=LocatedCity)
    async def network_location():
        # This local-only app uses the machine's outbound IP, not a remote browser's IP.
        return await services().providers.locate_city()

    @app.get("/sessions/{session_id}", response_model=SessionState)
    def session_state(session_id: SessionID):
        return services().store.get_session(session_id)

    @app.post("/sessions/{session_id}/search", response_model=SearchResult)
    async def search(session_id: SessionID, filters: ExploreFilters):
        svc = services()
        async with svc.lock(session_id):
            state = svc.store.get_session(session_id)
            anchor = svc.find_place(session_id, filters.anchor_id) if filters.anchor_id else None
            if filters.anchor_id and (not anchor or not anchor.location or anchor.city != filters.city):
                raise HTTPException(422, "当前起点不存在、缺少位置或不属于该城市，请重新选择。")
            result = await svc.providers.search_places(filters, anchor)
            existing = {p.id: p for p in state.results if p.city == filters.city and p.mode == settings.app_mode}
            existing.update({p.id: p for p in result.places})
            state.results = list(existing.values())[-100:]
            if state.filters.city != filters.city:
                state.selected_place = None
            state.filters = filters
            if filters.budget_scope == "all":
                state.budgets = {"food": filters.budget, "fun": filters.budget}
            else:
                state.budgets[filters.budget_scope] = filters.budget
            state.revision += 1
            svc.store.save_session(session_id, state)
            return result

    @app.get("/sessions/{session_id}/places/{place_id}", response_model=Place)
    def place_detail(session_id: SessionID, place_id: str):
        place = services().find_place(session_id, place_id)
        if not place:
            raise HTTPException(404, "未找到该场所，请重新查询。")
        return place

    @app.get("/sessions/{session_id}/favorites", response_model=list[Place])
    def favorites(session_id: SessionID):
        return [p for p in services().store.favorites(session_id) if p.mode == settings.app_mode]

    @app.put("/sessions/{session_id}/favorites/{place_id}")
    async def save_favorite(session_id: SessionID, place_id: str):
        svc = services()
        async with svc.lock(session_id):
            place = svc.find_place(session_id, place_id)
            if not place:
                raise HTTPException(404, "只能收藏已查询到的场所。")
            svc.store.save_favorite(session_id, place)
        return {"saved": True}

    @app.delete("/sessions/{session_id}/favorites/{place_id}")
    async def delete_favorite(session_id: SessionID, place_id: str):
        svc = services()
        async with svc.lock(session_id):
            svc.store.remove_favorite(session_id, place_id)
        return {"saved": False}

    @app.get("/sessions/{session_id}/preferences")
    def preferences(session_id: SessionID):
        return {"preferences": services().store.preferences(session_id)}

    @app.put("/sessions/{session_id}/preferences")
    async def save_preferences(session_id: SessionID, payload: PreferenceUpdate):
        svc = services()
        preferences = list(dict.fromkeys(p.strip()[:40] for p in payload.preferences if p.strip()))
        async with svc.lock(session_id):
            previous = svc.store.preferences(session_id)
            svc.store.save_preferences(session_id, preferences)
            state = svc.store.get_session(session_id)
            state.filters.preferences = list(dict.fromkeys([p for p in state.filters.preferences if p not in previous] + preferences))
            state.revision += 1
            svc.store.save_session(session_id, state)
        return {"preferences": preferences, "active_preferences": state.filters.preferences}

    @app.post("/chat", response_model=ChatResponse)
    async def chat(payload: ChatRequest):
        svc = services()
        async with svc.lock(payload.session_id):
            state = svc.store.get_session(payload.session_id)
            if payload.context:
                if payload.context.city != state.filters.city:
                    state.selected_place = None
                    state.results = []
                state.filters = payload.context
                scope = payload.context.budget_scope
                if scope == "all":
                    state.budgets = {"food": payload.context.budget, "fun": payload.context.budget}
                else:
                    state.budgets[scope] = payload.context.budget
            if not state.messages:
                state.filters.preferences = list(dict.fromkeys(state.filters.preferences + svc.store.preferences(payload.session_id)))
            if payload.page:
                if payload.page != state.page and payload.page in ("food", "fun"):
                    state.last_agents = [payload.page]
                state.page = payload.page
            if payload.selected_place_id:
                selected = svc.find_place(payload.session_id, payload.selected_place_id)
                if not selected or selected.city != state.filters.city:
                    raise HTTPException(422, "选中的地点无效，请重新选择。")
                state.selected_place = selected
            elif state.filters.anchor_id is None:
                state.selected_place = None
            output = await svc.agents.run(payload.message, state)
            plan = output["plan"]
            state.filters = plan.filters
            if plan.filters.budget_scope == "all":
                state.budgets = {"food": plan.filters.budget, "fun": plan.filters.budget}
            else:
                state.budgets[plan.filters.budget_scope] = plan.filters.budget
            state.last_agents = plan.agents
            if state.selected_place and state.selected_place.city != state.filters.city:
                state.selected_place = None
            results = sorted(output["results"], key=lambda r: ["city", "food", "fun"].index(r.agent))
            places = [p for result in results for p in result.places]
            # Only validated provider objects enter the final factual result and favorites cache.
            state.results = list({p.id: p for p in places}.values())
            state.messages = (state.messages + [{"role": "user", "content": payload.message}, {"role": "assistant", "content": output["answer"]}])[-20:]
            state.revision += 1
            svc.store.save_session(payload.session_id, state)
            notices = list(dict.fromkeys(output.get("notices", []) + [n for r in results for n in r.notices]))
            return ChatResponse(answer=output["answer"], session=state, agents=[r.agent for r in results], results=results, notices=notices, mode=settings.app_mode)

    return app


app = create_app()
