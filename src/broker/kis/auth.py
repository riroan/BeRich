import aiohttp
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class KISAuth:
    """KIS API Authentication Manager"""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        account_no: str,
        base_url: str,
    ):
        self.app_key = app_key
        self.app_secret = app_secret
        self.account_no = account_no
        self.base_url = base_url

        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self._websocket_key: Optional[str] = None

    @property
    def is_authenticated(self) -> bool:
        """Check if currently authenticated"""
        if not self._access_token:
            return False
        if self._token_expires_at is None or datetime.now() >= self._token_expires_at:
            return False
        return True

    async def authenticate(self, session: aiohttp.ClientSession) -> None:
        """Get OAuth access token"""
        endpoint = "/oauth2/tokenP"

        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }

        async with session.post(f"{self.base_url}{endpoint}", json=body) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"Authentication failed: {text}")

            data = await resp.json()
            self._access_token = data["access_token"]
            expires_in = int(data.get("expires_in", 86400))
            self._token_expires_at = datetime.now() + timedelta(seconds=expires_in - 60)

            logger.info("KIS authentication successful")

    async def get_websocket_key(self, session: aiohttp.ClientSession) -> str:
        """Get WebSocket approval key"""
        if self._websocket_key:
            return self._websocket_key

        endpoint = "/oauth2/Approval"

        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret,
        }

        async with session.post(f"{self.base_url}{endpoint}", json=body) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"WebSocket key request failed: {text}")

            data = await resp.json()
            self._websocket_key = data["approval_key"]
            return self._websocket_key

    async def ensure_authenticated(self, session: aiohttp.ClientSession) -> None:
        """Ensure valid authentication, refresh if needed"""
        if not self.is_authenticated:
            await self.authenticate(session)

    def get_headers(self, tr_id: str) -> dict:
        """Get API request headers"""
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def get_hash_key(self, session: aiohttp.ClientSession, body: dict) -> str:
        """Get hash key for order requests (if needed)"""
        # KIS requires hash key for certain order requests
        # Implementation depends on specific requirements
        pass
