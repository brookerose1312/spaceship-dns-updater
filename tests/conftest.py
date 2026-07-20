import logging

import pytest

from updater import Config, EmailConfig, shutdown_event


@pytest.fixture
def config():
    return Config(
        hosts=["@"],
        domain="example.com",
        api_key="api-key",
        api_secret="api-secret",
    )


@pytest.fixture
def config_with_email():
    return Config(
        hosts=["@"],
        domain="example.com",
        api_key="api-key",
        api_secret="api-secret",
        email=EmailConfig(
            smtp_host="smtp.example.com",
            smtp_port=587,
            user="email@example.com",
            password="email-password",
            recipient="recipient@example.com",
        ),
    )


@pytest.fixture
def config_multi_host():
    return Config(
        hosts=["@", "*"],
        domain="example.com",
        api_key="api-key",
        api_secret="api-secret",
    )


@pytest.fixture(autouse=True)
def _clear_shutdown():
    shutdown_event.clear()
    yield
    shutdown_event.clear()


@pytest.fixture(autouse=True)
def _clean_logger():
    log = logging.getLogger("spaceship-dns-updater")
    original_handlers = log.handlers.copy()
    original_level = log.level
    yield
    for handler in log.handlers:
        if handler not in original_handlers:
            handler.close()
    log.handlers = original_handlers
    log.level = original_level
