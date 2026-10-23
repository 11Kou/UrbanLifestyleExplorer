from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_mode: Literal["demo", "live"] = "demo"
    database_url: str = "sqlite:///./data/city-life.db"
    api_base_url: str = "http://127.0.0.1:8000"
    amap_api_key: SecretStr = SecretStr("")
    bocha_api_key: SecretStr = SecretStr("")
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen-plus"
    request_timeout: float = Field(default=20, gt=0, le=120)

    @property
    def has_llm(self) -> bool:
        return bool(self.llm_api_key.get_secret_value())

    @property
    def has_city_search(self) -> bool:
        return bool(self.bocha_api_key.get_secret_value()) or (self.has_llm and urlsplit(self.llm_base_url).hostname == "api.deepseek.com")


@lru_cache
def get_settings() -> Settings:
    return Settings()

