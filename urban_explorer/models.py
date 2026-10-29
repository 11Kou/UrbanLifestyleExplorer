from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Source(BaseModel):
    title: str
    url: HttpUrl | None = None
    kind: Literal["demo", "map", "article", "guide"] = "article"
    published_at: str | None = None
    checked_at: str = Field(default_factory=now_iso)
    excerpt: str = Field(default="", max_length=1600)


class Coordinates(BaseModel):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)


class LocatedCity(BaseModel):
    city: str
    province: str = ""
    district: str = ""
    source: Literal["browser", "ip"]


class PlacePhoto(BaseModel):
    url: HttpUrl
    title: str = "地点图片"


class Place(BaseModel):
    id: str
    city: str
    name: str
    kind: Literal["food", "fun"]
    category: str
    area: str = ""
    address: str = ""
    cost: float | None = Field(default=None, ge=0)
    rating: float | None = Field(default=None, ge=0, le=5)
    opening_hours: str | None = None
    tags: list[str] = Field(default_factory=list)
    reason: str = ""
    supervisor_reason: str = ""
    match_reasons: list[str] = Field(default_factory=list)
    unverified_preferences: list[str] = Field(default_factory=list)
    location: Coordinates | None = None
    distance_m: float | None = None
    sources: list[Source] = Field(default_factory=list)
    photos: list[PlacePhoto] = Field(default_factory=list, max_length=4)
    mode: Literal["demo", "live"]


class Area(BaseModel):
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)


class CityGuide(BaseModel):
    city: str
    introduction: str
    tags: list[str] = Field(default_factory=list)
    areas: list[Area] = Field(default_factory=list)
    foods: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    mode: Literal["demo", "live"]
    notices: list[str] = Field(default_factory=list)


class ExploreFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: str = Field(default="成都", min_length=1, max_length=40)
    area: str = Field(default="", max_length=60)
    kind: Literal["food", "fun"] = "food"
    category: str = Field(default="", max_length=40)
    query: str = Field(default="", max_length=100)
    budget: float | None = Field(default=None, gt=0, le=100000)
    budget_scope: Literal["food", "fun", "all"] = "food"
    preferences: list[str] = Field(default_factory=list, max_length=20)
    anchor_id: str | None = None
    radius_m: int = Field(default=3000, ge=100, le=50000)

    @field_validator("city")
    @classmethod
    def clean_city(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请输入城市")
        return value.removesuffix("市")


class SearchResult(BaseModel):
    places: list[Place]
    notices: list[str] = Field(default_factory=list)
    mode: Literal["demo", "live"]


class AgentResult(BaseModel):
    agent: Literal["city", "food", "fun"]
    summary: str
    places: list[Place] = Field(default_factory=list)
    guide: CityGuide | None = None
    sources: list[Source] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    attempts: int = Field(default=1, ge=1, le=2)


class SessionState(BaseModel):
    filters: ExploreFilters = Field(default_factory=ExploreFilters)
    page: Literal["home", "food", "fun", "saved"] = "home"
    messages: list[dict[str, str]] = Field(default_factory=list)
    results: list[Place] = Field(default_factory=list)
    selected_place: Place | None = None
    last_agents: list[Literal["city", "food", "fun"]] = Field(default_factory=list)
    budgets: dict[Literal["food", "fun"], float | None] = Field(default_factory=lambda: {"food": None, "fun": None})
    domain_filters: dict[Literal["food", "fun"], ExploreFilters] = Field(default_factory=dict)
    revision: int = 0


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=16, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    message: str = Field(min_length=1, max_length=2000)
    context: ExploreFilters | None = None
    page: Literal["home", "food", "fun", "saved"] | None = None
    selected_place_id: str | None = None

    @field_validator("message")
    @classmethod
    def clean_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请输入问题")
        return value


class ChatResponse(BaseModel):
    answer: str
    session: SessionState
    agents: list[str]
    results: list[AgentResult]
    notices: list[str]
    mode: Literal["demo", "live"]


class PreferenceUpdate(BaseModel):
    preferences: list[str] = Field(max_length=20)

