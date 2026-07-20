import logging
import smtplib
from logging.handlers import RotatingFileHandler
from unittest.mock import MagicMock, call, patch

import requests

from updater import (
    get_dns_records,
    get_ip_address,
    get_latest_logs,
    handle_signal,
    send_email,
    setup_logging,
    shutdown_event,
    update_dyndns,
)


def mock_response(status=200, json_data=None, text=""):
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.ok = 200 <= status < 400
    response.text = text
    if json_data is None:
        response.json.side_effect = requests.JSONDecodeError("invalid", "", 0)
    else:
        response.json.return_value = json_data
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(str(status))
    return response


class TestGetIpAddress:
    def test_success(self):
        response = mock_response(json_data={"ip": "1.2.3.4"})
        with patch("updater.requests.get", return_value=response):
            assert get_ip_address() == "1.2.3.4"

    def test_rejects_ipv6(self):
        response = mock_response(json_data={"ip": "2001:db8::1"})
        with patch("updater.requests.get", return_value=response):
            assert get_ip_address() is None

    def test_network_error(self):
        with patch(
            "updater.requests.get", side_effect=requests.ConnectionError("offline")
        ):
            assert get_ip_address() is None


class TestGetDnsRecords:
    def test_fetches_records_with_authentication(self, config):
        response = mock_response(json_data={"items": [], "total": 0})
        with patch("updater.requests.get", return_value=response) as request:
            assert get_dns_records(config) == []
        request.assert_called_once_with(
            config.records_url,
            headers=config.api_headers,
            params={"take": 500, "skip": 0, "orderBy": "name"},
            timeout=30,
        )

    def test_paginates(self, config):
        responses = [
            mock_response(json_data={"items": [{"name": "@"}], "total": 2}),
            mock_response(json_data={"items": [{"name": "www"}], "total": 2}),
        ]
        with patch("updater.requests.get", side_effect=responses) as request:
            records = get_dns_records(config)
        assert records == [{"name": "@"}, {"name": "www"}]
        assert request.call_args_list[1].kwargs["params"]["skip"] == 1

    def test_api_error(self, config, caplog):
        response = mock_response(status=401, json_data={"detail": "Unauthorized"})
        with (
            patch("updater.requests.get", return_value=response),
            caplog.at_level(logging.WARNING, logger="spaceship-dns-updater"),
        ):
            assert get_dns_records(config) is None
        assert "Unauthorized" in caplog.text

    def test_invalid_response(self, config):
        response = mock_response(json_data={"unexpected": []})
        with patch("updater.requests.get", return_value=response):
            assert get_dns_records(config) is None


class TestUpdateDyndns:
    def test_exact_record_match_is_read_only(self, config):
        records = [
            {
                "type": "A",
                "name": "@",
                "address": "1.2.3.4",
                "ttl": 300,
                "group": {"type": "custom"},
            }
        ]
        with (
            patch(
                "updater.requests.get",
                return_value=mock_response(json_data={"items": records, "total": 1}),
            ),
            patch("updater.requests.put") as put,
            patch("updater.requests.delete") as delete,
        ):
            assert update_dyndns(config, "@", "1.2.3.4") is True
        put.assert_not_called()
        delete.assert_not_called()

    def test_ttl_drift_updates_current_record(self, config):
        records = [
            {
                "type": "A",
                "name": "@",
                "address": "1.2.3.4",
                "ttl": 600,
                "group": {"type": "custom"},
            }
        ]
        with (
            patch(
                "updater.requests.get",
                return_value=mock_response(json_data={"items": records, "total": 1}),
            ),
            patch(
                "updater.requests.put", return_value=mock_response(status=204)
            ) as put,
            patch("updater.requests.delete") as delete,
        ):
            assert update_dyndns(config, "@", "1.2.3.4") is True
        assert put.call_args.kwargs["json"]["items"][0]["ttl"] == 300
        delete.assert_not_called()

    def test_creates_missing_record(self, config):
        responses = [
            mock_response(json_data={"items": [], "total": 0}),
            mock_response(status=204),
        ]
        with (
            patch("updater.requests.get", return_value=responses[0]),
            patch("updater.requests.put", return_value=responses[1]) as put,
            patch("updater.requests.delete") as delete,
        ):
            assert update_dyndns(config, "@", "1.2.3.4") is True
        put.assert_called_once_with(
            config.records_url,
            headers=config.api_headers,
            json={
                "force": False,
                "items": [{"type": "A", "name": "@", "address": "1.2.3.4", "ttl": 300}],
            },
            timeout=30,
        )
        delete.assert_not_called()

    def test_saves_new_address_before_deleting_stale_records(self, config):
        records = [
            {
                "type": "A",
                "name": "@",
                "address": "1.1.1.1",
                "ttl": 300,
                "group": {"type": "custom"},
            },
            {
                "type": "A",
                "name": "@",
                "address": "2.2.2.2",
                "ttl": 300,
                "group": {"type": "custom"},
            },
        ]
        calls = []

        def save(*_args, **_kwargs):
            calls.append("put")
            return mock_response(status=204)

        def delete(*_args, **_kwargs):
            calls.append("delete")
            return mock_response(status=204)

        with (
            patch(
                "updater.requests.get",
                return_value=mock_response(json_data={"items": records, "total": 2}),
            ),
            patch("updater.requests.put", side_effect=save),
            patch("updater.requests.delete", side_effect=delete) as delete_request,
        ):
            assert update_dyndns(config, "@", "3.3.3.3") is True
        assert calls == ["put", "delete"]
        assert delete_request.call_args.kwargs["json"] == [
            {"type": "A", "name": "@", "address": "1.1.1.1"},
            {"type": "A", "name": "@", "address": "2.2.2.2"},
        ]

    def test_keeps_current_record_and_removes_only_stale_record(self, config):
        records = [
            {"type": "A", "name": "@", "address": "1.2.3.4", "ttl": 300},
            {"type": "A", "name": "@", "address": "4.3.2.1"},
        ]
        with (
            patch(
                "updater.requests.get",
                return_value=mock_response(json_data={"items": records, "total": 2}),
            ),
            patch("updater.requests.put") as put,
            patch(
                "updater.requests.delete", return_value=mock_response(status=204)
            ) as delete,
        ):
            assert update_dyndns(config, "@", "1.2.3.4") is True
        assert delete.call_args.kwargs["json"] == [
            {"type": "A", "name": "@", "address": "4.3.2.1"}
        ]
        put.assert_not_called()

    def test_refuses_product_managed_record(self, config):
        records = [
            {
                "type": "A",
                "name": "@",
                "address": "1.1.1.1",
                "group": {"type": "product"},
            }
        ]
        with (
            patch(
                "updater.requests.get",
                return_value=mock_response(json_data={"items": records, "total": 1}),
            ),
            patch("updater.requests.put") as put,
            patch("updater.requests.delete") as delete,
        ):
            assert update_dyndns(config, "@", "1.2.3.4") is False
        put.assert_not_called()
        delete.assert_not_called()

    def test_save_failure_does_not_delete_old_record(self, config):
        records = [{"type": "A", "name": "@", "address": "1.1.1.1"}]
        with (
            patch(
                "updater.requests.get",
                return_value=mock_response(json_data={"items": records, "total": 1}),
            ),
            patch(
                "updater.requests.put",
                return_value=mock_response(
                    status=422, json_data={"detail": "Conflict"}
                ),
            ),
            patch("updater.requests.delete") as delete,
        ):
            assert update_dyndns(config, "@", "1.2.3.4") is False
        delete.assert_not_called()

    def test_delete_failure_is_reported(self, config):
        records = [{"type": "A", "name": "@", "address": "1.1.1.1"}]
        with (
            patch(
                "updater.requests.get",
                return_value=mock_response(json_data={"items": records, "total": 1}),
            ),
            patch("updater.requests.put", return_value=mock_response(status=204)),
            patch(
                "updater.requests.delete",
                return_value=mock_response(status=500, text="error"),
            ),
        ):
            assert update_dyndns(config, "@", "1.2.3.4") is False


class TestUtilities:
    def test_latest_logs(self, tmp_path):
        log = tmp_path / "test.log"
        lines = [f"line {index}\n" for index in range(20)]
        log.write_text("".join(lines))
        assert get_latest_logs(str(log)) == "".join(lines[-10:])

    def test_logging(self, tmp_path):
        setup_logging(str(tmp_path / "test.log"))
        handler_types = [
            type(handler)
            for handler in logging.getLogger("spaceship-dns-updater").handlers
        ]
        assert logging.StreamHandler in handler_types
        assert RotatingFileHandler in handler_types

    def test_signal_sets_shutdown_event(self):
        handle_signal(15, None)
        assert shutdown_event.is_set()


class TestSendEmail:
    def test_sends_email(self, config_with_email):
        server = MagicMock()
        with patch("updater.smtplib.SMTP") as smtp:
            smtp.return_value.__enter__.return_value = server
            send_email(config_with_email, "1.2.3.4", results={"@": True})
        assert server.method_calls[:3] == [
            call.ehlo(),
            call.starttls(context=server.starttls.call_args.kwargs["context"]),
            call.login("email@example.com", "email-password"),
        ]
        message = server.send_message.call_args.args[0]
        assert "Spaceship DNS update succeeded" in message["Subject"]

    def test_smtp_error_is_caught(self, config_with_email):
        with patch("updater.smtplib.SMTP", side_effect=smtplib.SMTPException("failed")):
            send_email(config_with_email, "1.2.3.4", results={"@": False})

    def test_disabled(self, config):
        with patch("updater.smtplib.SMTP") as smtp:
            send_email(config, "1.2.3.4", results={"@": True})
        smtp.assert_not_called()
