from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    DATABASE_URL: str
    LOG_LEVEL: str = "INFO"

    STRAVA_CLIENT_ID: str = ""
    STRAVA_CLIENT_SECRET: str = ""
    STRAVA_REFRESH_TOKEN: str = ""

    ATHLETE_FTP: int = 200


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
