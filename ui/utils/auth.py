import chainlit as cl


def setup_oauth() -> None:
    """Sets up OAuth authentication."""

    @cl.oauth_callback
    async def oauth_callback(
        provider_id: str,
        token: str,
        raw_user_data: dict[str, str],
        default_user: cl.User,
    ) -> cl.User:

        default_user.metadata = {
            "token": token,
            "email": raw_user_data["email"],
        }

        return default_user
