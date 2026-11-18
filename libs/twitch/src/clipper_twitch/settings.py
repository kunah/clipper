from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class TwitchSettings(BaseSettings):
    """CLIPPER_TWITCH_* env vars. Credentials come from a Twitch app (dev.twitch.tv/console).

    Only the Helix API needs them; reading chat works anonymously.
    """

    model_config = SettingsConfigDict(env_prefix="CLIPPER_TWITCH_", env_file=".env", extra="ignore")

    client_id: str | None = None
    client_secret: SecretStr | None = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret and self.client_secret.get_secret_value())
