from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
        # An unset GitHub Actions `vars.*`/`secrets.*` still sets the env var to
        # "" rather than leaving it absent, which would otherwise fail typed
        # (e.g. int) fields instead of falling back to their default.
        env_ignore_empty=True,
    )

    DATABASE_URL: str
    LOG_LEVEL: str = "INFO"

    # OAuth for the MCP server. Claude.ai connectors authenticate over OAuth
    # (DCR + PKCE), so the server logs users in via a GitHub OAuth app and only
    # admits the allowlisted logins. All four must be set for auth to engage;
    # when any is missing the server runs open (see app.py) and warns loudly.
    MCP_GITHUB_CLIENT_ID: str = ""
    MCP_GITHUB_CLIENT_SECRET: str = ""
    MCP_PUBLIC_URL: str = ""  # public origin, e.g. https://<app>.onrender.com
    MCP_ALLOWED_GITHUB_LOGINS: str = ""  # comma-separated GitHub usernames

    STRAVA_CLIENT_ID: str = ""
    STRAVA_CLIENT_SECRET: str = ""
    STRAVA_REFRESH_TOKEN: str = ""

    WITHINGS_CLIENT_ID: str = ""
    WITHINGS_CLIENT_SECRET: str = ""
    WITHINGS_ACCESS_TOKEN: str = ""
    WITHINGS_REFRESH_TOKEN: str = ""
    WITHINGS_USERID: str = ""

    GARMIN_EMAIL: str = ""
    GARMIN_PASSWORD: str = ""
    GARMINTOKENS_B64: str = ""

    ATHLETE_FTP: int = 200
    ATHLETE_HR_REST: int = 49
    ATHLETE_HR_MAX: int = 193

    NOTION_TOKEN: str = ""
    NOTION_DB_ACTIVITIES_ID: str = ""
    NOTION_DB_PLAN_ID: str = ""
    NOTION_DB_METRICS_ID: str = ""

    WEATHER_LATITUDE: float = 58.4658
    WEATHER_LONGITUDE: float = 8.8512
    WEATHER_TIMEZONE: str = "Europe/Oslo"


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
