from functools import lru_cache
from typing import Literal
from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    paypal_client_id: SecretStr = SecretStr('')
    paypal_client_secret: SecretStr = SecretStr('')
    paypal_base_url: Literal['https://api-m.sandbox.paypal.com'] = 'https://api-m.sandbox.paypal.com'
    # Any OpenAI-compatible chat completions endpoint (OpenAI, an LLM gateway, a local server).
    llm_base_url: AnyHttpUrl = 'https://api.openai.com/v1'
    llm_api_key: SecretStr = SecretStr('')
    llm_model: str = 'gpt-4.1-mini'
    llm_timeout_s: float = 45
    database_url: str = 'sqlite:///./data/payguard.db'
    backend_url: AnyHttpUrl = 'http://127.0.0.1:8000'
    # On Render, RENDER_EXTERNAL_URL is the public URL of the dashboard.
    dashboard_url: AnyHttpUrl = Field('http://127.0.0.1:8501',
                                      validation_alias=AliasChoices('DASHBOARD_URL', 'RENDER_EXTERNAL_URL'))
    # Where PayPal sends the buyer back: the backend (local default) or the dashboard, which then
    # calls the backend. Use 'dashboard' when only the dashboard is public, as on Render.
    paypal_return_to: Literal['backend', 'dashboard'] = 'backend'
    catalog_path: str = 'data/catalog.json'

    @field_validator('database_url')
    @classmethod
    def sqlite_only(cls, value: str) -> str:
        if not value.startswith('sqlite:///') or not value.removeprefix('sqlite:///'):
            raise ValueError('Use a file-backed sqlite:/// database URL')
        if value == 'sqlite:///:memory:':
            raise ValueError('Use a file-backed database for persistent audit records')
        return value

    @property
    def paypal_configured(self) -> bool:
        return bool(self.paypal_client_id.get_secret_value() and self.paypal_client_secret.get_secret_value())

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key.get_secret_value() and self.llm_model)


@lru_cache
def get_settings() -> Settings:
    return Settings()
