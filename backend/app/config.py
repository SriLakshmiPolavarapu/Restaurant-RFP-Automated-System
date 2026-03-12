from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    GEMINI_API_KEY: str
    GEMINI_MODEL: str = "gemini-2.5-flash"

    USDA_API_KEY: str | None = None
    USDA_MMN_API_KEY: str
    USDA_MARS_BASE_URL: str = "https://marsapi.ams.usda.gov/services/v1.2"
    USDA_MARS_REPORT_IDS: str = "1095,1280"

    RESTAURANT_CITY: str = "San Francisco"
    RESTAURANT_STATE: str = "CA"

    NOMINATIM_BASE_URL: str = "https://nominatim.openstreetmap.org"
    OVERPASS_URL: str = "https://overpass-api.de/api/interpreter"
    APP_USER_AGENT: str = "pathway-rfp-demo/1.0"

    DATABASE_URL: str = "sqlite:///./pathway_rfp.db"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()