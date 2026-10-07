"""Single-worker authentication limits and production configuration safeguards."""

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from pydantic import ValidationError

from app.config.settings import Settings, settings
from app.main import app
from app.services import auth_service as auth
from app.shared.errors import NonceInvalidError, RateLimitError


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    monkeypatch.setattr(auth, "_nonces", {})
    monkeypatch.setattr(auth, "_rates", {})


def test_expired_entries_cleaned_and_nonce_capacity_bounded(monkeypatch):
    monkeypatch.setattr(settings, "nonce_max_entries", 1)
    auth._nonces["old"] = ("old", "old", time.time() - 1)
    auth._rates["old"] = (time.monotonic() - 1, 30)
    auth.issue_nonce("0x" + "1" * 40)
    assert "old" not in auth._nonces and not auth._rates
    with pytest.raises(RateLimitError):
        auth.issue_nonce("0x" + "2" * 40)
    assert len(auth._nonces) == 1


def test_nonce_consumed_only_once_under_concurrency():
    account = Account.create()
    _, message, _ = auth.issue_nonce(account.address)
    signature = account.sign_message(encode_defunct(text=message)).signature.hex()

    def verify(_):
        try:
            auth.verify_login(account.address, signature)
            return True
        except NonceInvalidError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(verify, range(4))) == 1


def test_nonce_expiry_is_enforced():
    address = "0x" + "1" * 40
    auth._nonces[address] = ("x", "x", time.time() - 1)
    with pytest.raises(NonceInvalidError):
        auth.verify_login(address, "bad")
    assert not auth._nonces


async def test_nonce_and_verify_share_peer_rate_limit(monkeypatch):
    monkeypatch.setattr(settings, "auth_rate_per_minute", 1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/v1/auth/nonce", json={"address": "0x" + "1" * 40})
        assert first.status_code == 200
        second = await client.post(
            "/api/v1/auth/verify", json={"address": "0x" + "1" * 40, "signature": "0x" + "0" * 130}
        )
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limited"


def test_rate_window_expires_and_storage_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "auth_rate_per_minute", 1)
    monkeypatch.setattr(settings, "auth_rate_max_entries", 1)
    auth.check_auth_rate("one")
    with pytest.raises(RateLimitError):
        auth.check_auth_rate("two")
    auth._rates["one"] = (time.monotonic() - 1, 1)
    auth.check_auth_rate("two")
    assert len(auth._rates) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"jwt_secret": "dev-only-insecure-secret-change-me-before-deploy"},
        {"jwt_secret": ""},
        {"jwt_secret": "short"},
        {"debug": True},
        {"siwe_domain": "localhost:5173"},
        {"cors_origins": "*"},
    ],
)
def test_production_rejects_insecure_settings(override):
    options = dict(
        environment="production",
        debug=False,
        jwt_secret="a" * 48,
        siwe_domain="copilot.example.com",
        cors_origins="https://copilot.example.com",
    )
    options.update(override)
    with patch.dict("os.environ", {}, clear=True), pytest.raises(ValidationError):
        Settings(_env_file=None, **options)


def test_valid_production_settings():
    with patch.dict("os.environ", {}, clear=True):
        result = Settings(
            _env_file=None,
            environment="production",
            debug=False,
            jwt_secret="a" * 48,
            siwe_domain="copilot.example.com",
            cors_origins="https://copilot.example.com",
        )
    assert result.environment == "production"
