from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Twitch's own public client of the old mobile app: GQL answers chat-replay cursor queries from
# the web client id with "failed integrity check" since 2023-05; TwitchDownloader, twitch-dl and
# chat-downloader still use this one (research 2026-11-19).
DEFAULT_GQL_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
# Persisted query `VideoCommentsByOffsetOrCursor` (the hash all three tools use in 2026).
DEFAULT_GQL_COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


class TwitchSettings(BaseSettings):
    """CLIPPER_TWITCH_* env vars. Credentials come from a Twitch app (dev.twitch.tv/console).

    Only the Helix API needs them; reading chat works anonymously. The GQL fields configure the
    anonymous VOD chat replay (`clipper_twitch.gql`): an undocumented API, so a changed client id
    or query hash can be fixed by env without a release.
    """

    model_config = SettingsConfigDict(env_prefix="CLIPPER_TWITCH_", env_file=".env", extra="ignore")

    client_id: str | None = None
    client_secret: SecretStr | None = None
    gql_client_id: str = DEFAULT_GQL_CLIENT_ID
    gql_comments_hash: str = DEFAULT_GQL_COMMENTS_HASH

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret and self.client_secret.get_secret_value())
