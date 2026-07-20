from typing import ClassVar
from unittest.mock import patch

import pytest

from updater import Config, EmailConfig


class TestConfig:
    def test_defaults(self):
        config = Config(
            hosts=["@"], domain="example.com", api_key="key", api_secret="secret"
        )
        assert config.ttl == 300
        assert config.check_interval == 5
        assert config.force_update_days == 2
        assert config.hostname == "example.com"
        assert config.records_url.endswith("/dns/records/example.com")

    def test_multiple_hostnames(self):
        config = Config(
            hosts=["@", "*", "www"],
            domain="example.com",
            api_key="key",
            api_secret="secret",
        )
        assert config.hostname == "example.com, *.example.com, www.example.com"

    def test_api_headers(self):
        config = Config(
            hosts=["@"], domain="example.com", api_key="key", api_secret="secret"
        )
        assert config.api_headers == {
            "X-API-Key": "key",
            "X-API-Secret": "secret",
        }

    def test_credentials_are_hidden_from_repr(self):
        config = Config(
            hosts=["@"], domain="example.com", api_key="key", api_secret="secret"
        )
        assert "key" not in repr(config)
        assert "secret" not in repr(config)

    def test_force_update_checks(self):
        config = Config(
            hosts=["@"],
            domain="example.com",
            api_key="key",
            api_secret="secret",
            check_interval=10,
            force_update_days=1,
        )
        assert config.force_update_checks == 144


class TestConfigFromEnv:
    required: ClassVar[dict[str, str]] = {
        "SPACESHIP_HOST": "@",
        "SPACESHIP_DOMAIN": "example.com",
        "SPACESHIP_API_KEY": "key",
        "SPACESHIP_API_SECRET": "secret",
    }

    def test_required_only(self):
        with patch.dict("os.environ", self.required, clear=True):
            config = Config.from_env()
        assert config.hosts == ["@"]
        assert config.domain == "example.com"
        assert config.api_key == "key"
        assert config.api_secret == "secret"

    def test_optional_values(self):
        environment = {
            **self.required,
            "SPACESHIP_HOST": "@, *, @",
            "SPACESHIP_TTL": "600",
            "CHECK_INTERVAL_MINUTES": "10",
            "FORCE_UPDATE_DAYS": "3",
            "LOG_FILE": "/tmp/updater.log",
        }
        with patch.dict("os.environ", environment, clear=True):
            config = Config.from_env()
        assert config.hosts == ["@", "*"]
        assert config.ttl == 600
        assert config.check_interval == 10
        assert config.force_update_days == 3
        assert config.log_file == "/tmp/updater.log"

    @pytest.mark.parametrize("ttl", ["59", "3601"])
    def test_invalid_ttl(self, ttl):
        with (
            patch.dict(
                "os.environ", {**self.required, "SPACESHIP_TTL": ttl}, clear=True
            ),
            pytest.raises(ValueError, match="SPACESHIP_TTL"),
        ):
            Config.from_env()

    def test_empty_host_rejected(self):
        with (
            patch.dict(
                "os.environ",
                {**self.required, "SPACESHIP_HOST": "@,,*"},
                clear=True,
            ),
            pytest.raises(ValueError, match=r"SPACESHIP_HOST.*empty"),
        ):
            Config.from_env()

    def test_email_config(self):
        environment = {
            **self.required,
            "EMAIL_ENABLED": "true",
            "EMAIL_USER": "sender@example.com",
            "EMAIL_PASS": "password",
            "EMAIL_RECIPIENT": "recipient@example.com",
        }
        with patch.dict("os.environ", environment, clear=True):
            config = Config.from_env()
        assert config.email == EmailConfig(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            user="sender@example.com",
            password="password",
            recipient="recipient@example.com",
        )

    @pytest.mark.parametrize(
        ("missing", "expected"),
        [
            ("SPACESHIP_HOST", "SPACESHIP_HOST"),
            ("SPACESHIP_DOMAIN", "SPACESHIP_DOMAIN"),
            ("SPACESHIP_API_KEY", "SPACESHIP_API_KEY"),
            ("SPACESHIP_API_SECRET", "SPACESHIP_API_SECRET"),
        ],
    )
    def test_missing_required_value(self, missing, expected):
        environment = {
            key: value for key, value in self.required.items() if key != missing
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            pytest.raises(KeyError, match=expected),
        ):
            Config.from_env()
