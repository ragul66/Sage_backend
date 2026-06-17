from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "SageAssistant API"
    API_V1_STR: str = "/api"
    
    GEMINI_API_KEY: str = "your_gemini_api_key_here"
    GEMINI_MODEL: str = "gemini-2.5-flash"
    PORT: int = 8080
    
    # Optional Retell credentials for the intermediary integration
    RETELL_API_KEY: Optional[str] = None
    RETELL_AGENT_ID: Optional[str] = None
    
    CORS_ORIGINS: str = "*"

    @property
    def cors_origins_list(self) -> List[str]:
        if not self.CORS_ORIGINS:
            return []
        if self.CORS_ORIGINS == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )

settings = Settings()
