from unittest.mock import call, patch

import pytest

from updater import Config, main, run_force_update, run_polling_loop, shutdown_event


class TestRunForceUpdate:
    def test_success(self, config):
        with (
            patch("updater.get_ip_address", return_value="1.2.3.4"),
            patch("updater.reconcile_hosts", return_value={"@": True}) as reconcile,
            patch("updater.send_email") as email,
            pytest.raises(SystemExit, match="0"),
        ):
            run_force_update(config)
        reconcile.assert_called_once_with(config, "1.2.3.4")
        email.assert_called_once_with(config, "1.2.3.4", results={"@": True})

    def test_failure(self, config):
        with (
            patch("updater.get_ip_address", return_value="1.2.3.4"),
            patch("updater.reconcile_hosts", return_value={"@": False}),
            patch("updater.send_email"),
            pytest.raises(SystemExit, match="1"),
        ):
            run_force_update(config)

    def test_missing_ip(self, config):
        with (
            patch("updater.get_ip_address", return_value=None),
            pytest.raises(SystemExit, match="1"),
        ):
            run_force_update(config)


class TestRunPollingLoop:
    def test_reconciles_on_startup(self, config):
        with (
            patch("updater.get_ip_address", return_value="1.2.3.4"),
            patch("updater.reconcile_hosts", return_value={"@": True}) as reconcile,
            patch("updater.send_email") as email,
            patch.object(shutdown_event, "is_set", side_effect=[False, True]),
            patch.object(shutdown_event, "wait", return_value=False),
            patch("updater.Path"),
        ):
            run_polling_loop(config)
        reconcile.assert_called_once_with(config, "1.2.3.4")
        email.assert_called_once_with(config, "1.2.3.4", results={"@": True})

    def test_failed_startup_reconciliation_retries(self, config_multi_host):
        with (
            patch("updater.get_ip_address", return_value="1.2.3.4"),
            patch(
                "updater.reconcile_hosts",
                side_effect=[
                    {"@": True, "*": False},
                    {"@": True, "*": True},
                ],
            ) as reconcile,
            patch("updater.send_email") as email,
            patch.object(shutdown_event, "is_set", side_effect=[False, False, True]),
            patch.object(shutdown_event, "wait", return_value=False),
            patch("updater.Path"),
        ):
            run_polling_loop(config_multi_host)
        assert reconcile.call_args_list == [
            call(config_multi_host, "1.2.3.4"),
            call(config_multi_host, "1.2.3.4"),
        ]
        email.assert_called_once_with(
            config_multi_host,
            "1.2.3.4",
            results={"@": True, "*": True},
        )

    def test_unchanged_ip_is_not_reconciled_every_check(self, config):
        with (
            patch("updater.get_ip_address", return_value="1.2.3.4"),
            patch("updater.reconcile_hosts", return_value={"@": True}) as reconcile,
            patch("updater.send_email") as email,
            patch.object(shutdown_event, "is_set", side_effect=[False, False, True]),
            patch.object(shutdown_event, "wait", return_value=False),
            patch("updater.Path"),
        ):
            run_polling_loop(config)
        reconcile.assert_called_once_with(config, "1.2.3.4")
        email.assert_called_once_with(config, "1.2.3.4", results={"@": True})

    def test_ip_change_triggers_reconciliation(self, config):
        with (
            patch("updater.get_ip_address", side_effect=["1.1.1.1", "2.2.2.2"]),
            patch("updater.reconcile_hosts", return_value={"@": True}) as reconcile,
            patch("updater.send_email") as email,
            patch.object(shutdown_event, "is_set", side_effect=[False, False, True]),
            patch.object(shutdown_event, "wait", return_value=False),
            patch("updater.Path"),
        ):
            run_polling_loop(config)
        assert reconcile.call_args_list == [
            call(config, "1.1.1.1"),
            call(config, "2.2.2.2"),
        ]
        assert email.call_args_list == [
            call(config, "1.1.1.1", results={"@": True}),
            call(config, "2.2.2.2", results={"@": True}),
        ]

    def test_missing_initial_ip_retries_lookup_and_reconciles(self, config):
        with (
            patch("updater.get_ip_address", side_effect=[None, "1.2.3.4"]),
            patch("updater.reconcile_hosts", return_value={"@": True}) as reconcile,
            patch("updater.send_email") as email,
            patch.object(shutdown_event, "is_set", side_effect=[False, False, True]),
            patch.object(shutdown_event, "wait", return_value=False),
            patch("updater.Path"),
        ):
            run_polling_loop(config)
        reconcile.assert_called_once_with(config, "1.2.3.4")
        email.assert_called_once_with(config, "1.2.3.4", results={"@": True})


class TestMain:
    def test_force_flag(self):
        config = Config(
            hosts=["@"], domain="example.com", api_key="key", api_secret="secret"
        )
        with (
            patch("sys.argv", ["updater.py", "--force"]),
            patch("updater.Config.from_env", return_value=config),
            patch("updater.setup_logging"),
            patch("updater.run_force_update") as force,
        ):
            main()
        force.assert_called_once_with(config)

    def test_polling_mode(self):
        config = Config(
            hosts=["@"], domain="example.com", api_key="key", api_secret="secret"
        )
        with (
            patch("sys.argv", ["updater.py"]),
            patch("updater.Config.from_env", return_value=config),
            patch("updater.setup_logging"),
            patch("updater.run_polling_loop") as polling,
        ):
            main()
        polling.assert_called_once_with(config)
