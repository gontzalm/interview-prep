import os
from collections.abc import Generator
from typing import final, override

from botocore.credentials import Credentials
from botocore.session import Session
from httpx import Request, Response
from httpx_auth import AWS4Auth


@final
class AwsBotoAuth(AWS4Auth):
    def __init__(self, service: str = "lambda"):
        session = Session()
        self._refreshable_credentials: Credentials = session.get_credentials()
        super().__init__(
            "_",
            "_",
            os.environ["AWS_REGION"],
            service,
        )

    def refresh_credentials(self) -> None:
        credentials = self._refreshable_credentials.get_frozen_credentials()
        self.access_id = credentials.access_key
        self.secret_key = credentials.secret_key
        self.security_token = credentials.token

    @override
    def auth_flow(self, request: Request) -> Generator[Request, Response, None]:
        self.refresh_credentials()
        return super().auth_flow(request)
