"""Security regressions. All credentials and mail payloads are synthetic."""
import argparse
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import socket
import smtplib
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("protonmail_tool", ROOT / "scripts/protonmail_tool.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def resolved(address, port=1143):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


def arguments():
    # Legacy attributes deliberately remain: direct callers cannot bypass policy.
    return argparse.Namespace(username=None, local_bridge_tls=True,
                              imap_host=None, imap_port=None, smtp_host=None,
                              smtp_port=None, no_starttls=False)


class ConnectionSecurityTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "PROTONMAIL_CONFIG": "/dev/null", "PROTONMAIL_USERNAME": "dummy-user",
            "PROTONMAIL_PASSWORD": "dummy-password",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.config = patch.object(tool, "CONFIG_VALUES", None)
        self.config.start()
        self.addCleanup(self.config.stop)

    def test_cli_rejects_endpoint_and_plaintext_overrides_for_every_command(self):
        commands = [["list-folders"], ["search"], ["read", "--uid", "1"],
                    ["move", "--uid", "1"],
                    ["send", "--to", "nobody@example.invalid", "--subject", "dummy"]]
        overrides = [["--imap-host", "receiver.example.invalid"], ["--smtp-host", "receiver.example.invalid"],
                     ["--imap-port", "9999"], ["--smtp-port", "9999"], ["--no-starttls"],
                     ["--imap-h", "receiver.example.invalid"], ["--smtp-h", "receiver.example.invalid"],
                     ["--no-start"]]
        for command in commands:
            for override in overrides:
                with self.subTest(command=command, override=override), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as result:
                        tool.build_parser().parse_args(command + override)
                    self.assertEqual(result.exception.code, 2)

    def test_rejected_resolution_never_accesses_credentials_or_opens_socket(self):
        resolutions = [[resolved("203.0.113.1")], [resolved("::ffff:203.0.113.1")],
                       [resolved("127.0.0.1"), resolved("203.0.113.1")],
                       [resolved("::1"), resolved("2001:db8::1")], [],
                       socket.gaierror("unresolvable")]
        for protocol, connector in [("IMAP", tool.connect_imap), ("SMTP", tool.connect_smtp)]:
            for resolution in resolutions:
                with self.subTest(protocol=protocol, resolution=resolution):
                    with patch.dict(os.environ, {f"PROTONMAIL_{protocol}_HOST": "bridge.example.invalid"}), \
                         patch.object(tool.socket, "getaddrinfo", **({"side_effect": resolution} if isinstance(resolution, Exception) else {"return_value": resolution})), \
                         patch.object(tool, "credentials") as creds:
                        # Patch on module objects; no import under the synthetic name needed.
                        module, attribute = (tool.imaplib, "IMAP4") if protocol == "IMAP" else (tool.smtplib, "SMTP")
                        with patch.object(module, attribute) as client:
                            with self.assertRaises(SystemExit):
                                connector(arguments())
                            creds.assert_not_called()
                            client.assert_not_called()

    def test_config_remote_host_and_send_fail_before_credential_lookup(self):
        for protocol, command in [("IMAP", ["list-folders"]),
                                   ("SMTP", ["send", "--to", "nobody@example.invalid", "--subject", "dummy", "--body", "dummy"])]:
            with self.subTest(protocol=protocol), patch.object(tool, "CONFIG_VALUES", {f"PROTONMAIL_{protocol}_HOST": "203.0.113.1"}), \
                 patch.object(tool.socket, "getaddrinfo", return_value=[resolved("203.0.113.1")]), \
                 patch.object(tool, "credentials") as creds, \
                 patch.object(tool, "env", wraps=tool.env) as lookup, \
                 patch.object(tool.imaplib, "IMAP4") as imap, patch.object(tool.smtplib, "SMTP") as smtp:
                with self.assertRaises(SystemExit):
                    args = tool.build_parser().parse_args(command)
                    args.func(args)
                creds.assert_not_called()
                self.assertFalse(any(call.args[0] in {"PROTONMAIL_USERNAME", "PROTONMAIL_PASSWORD"} for call in lookup.call_args_list))
                imap.assert_not_called()
                smtp.assert_not_called()

    def test_loopback_resolution_is_pinned_and_prefers_ipv4(self):
        for answers, expected in [([resolved("127.0.0.1")], "127.0.0.1"),
                                  ([resolved("::1")], "::1"),
                                  ([resolved("::1"), resolved("127.0.0.1")], "127.0.0.1"),
                                  ([resolved("127.0.0.2")], "127.0.0.2")]:
            with self.subTest(answers=answers), patch.dict(os.environ, {"PROTONMAIL_IMAP_HOST": "localhost"}), \
                 patch.object(tool.socket, "getaddrinfo", side_effect=[answers, [resolved("203.0.113.1")]]) as dns, \
                 patch.object(tool.imaplib, "IMAP4") as factory:
                client = tool.connect_imap(arguments())
                factory.assert_called_once_with(expected, 1143)
                dns.assert_called_once_with("localhost", 1143, type=socket.SOCK_STREAM)
                client.login.assert_called_once_with("dummy-user", "dummy-password")

    def test_environment_precedence_and_custom_ports(self):
        for protocol, port in [("IMAP", 21143), ("SMTP", 21025)]:
            with self.subTest(protocol=protocol), patch.object(tool, "CONFIG_VALUES", {
                f"PROTONMAIL_{protocol}_HOST": "203.0.113.1", f"PROTONMAIL_{protocol}_PORT": "9999"}), \
                 patch.dict(os.environ, {f"PROTONMAIL_{protocol}_HOST": "localhost", f"PROTONMAIL_{protocol}_PORT": str(port)}), \
                 patch.object(tool.socket, "getaddrinfo", return_value=[resolved("127.0.0.1", port)]) as dns:
                self.assertEqual(tool.connection_settings(protocol), (["127.0.0.1"], port))
                dns.assert_called_once_with("localhost", port, type=socket.SOCK_STREAM)

    def test_ipv6_fallback_uses_only_validated_addresses_without_name_reresolution(self):
        for protocol, connector, module, attribute in [
            ("IMAP", tool.connect_imap, tool.imaplib, "IMAP4"),
            ("SMTP", tool.connect_smtp, tool.smtplib, "SMTP"),
        ]:
            client = Mock()
            with self.subTest(protocol=protocol), patch.dict(os.environ, {f"PROTONMAIL_{protocol}_HOST": "localhost"}), \
                 patch.object(tool.socket, "getaddrinfo", side_effect=[
                     [resolved("::1"), resolved("127.0.0.1"), resolved("::1")],
                     [resolved("203.0.113.1")]]) as dns, \
                 patch.object(module, attribute, side_effect=[ConnectionRefusedError("IPv4 refused"), client]) as factory:
                connector(arguments())
                self.assertEqual([call.args[0] for call in factory.call_args_list], ["127.0.0.1", "::1"])
                self.assertEqual(dns.call_count, 1)
                client.login.assert_called_once_with("dummy-user", "dummy-password")

    def test_all_connection_failures_propagate_without_credentials(self):
        for connector, module, attribute in [(tool.connect_imap, tool.imaplib, "IMAP4"),
                                             (tool.connect_smtp, tool.smtplib, "SMTP")]:
            with self.subTest(connector=connector.__name__), \
                 patch.object(tool.socket, "getaddrinfo", return_value=[resolved("127.0.0.1"), resolved("::1")]), \
                 patch.object(module, attribute, side_effect=[ConnectionRefusedError("IPv4 refused"), ConnectionRefusedError("IPv6 refused")]), \
                 patch.object(tool, "credentials") as creds:
                with self.assertRaisesRegex(ConnectionRefusedError, "IPv6 refused"):
                    connector(arguments())
                creds.assert_not_called()

    def test_config_file_fallback_custom_path_and_environment_credentials(self):
        # Only temporary dummy files, never the user's config directories.
        with tempfile.TemporaryDirectory() as directory:
            primary = str(Path(directory) / "missing.env")
            fallback = Path(directory) / "fallback.env"
            custom = Path(directory) / "custom.env"
            fallback.write_text('PROTONMAIL_IMAP_HOST="localhost"\nPROTONMAIL_IMAP_PORT=21143\nPROTONMAIL_USERNAME=config-dummy\nPROTONMAIL_PASSWORD=config-dummy-password\n')
            custom.write_text("PROTONMAIL_IMAP_HOST=::1\n")
            with patch.object(tool, "DEFAULT_CONFIG", primary), patch.object(tool, "FALLBACK_CONFIGS", (str(fallback),)):
                del os.environ["PROTONMAIL_CONFIG"]
                self.assertEqual(tool.env("PROTONMAIL_IMAP_HOST"), "localhost")
                self.assertEqual(tool.env("PROTONMAIL_IMAP_PORT"), "21143")
                self.assertEqual(tool.credentials(arguments()), ("dummy-user", "dummy-password"))
                tool.CONFIG_VALUES = None
                os.environ["PROTONMAIL_CONFIG"] = str(custom)
                self.assertEqual(tool.env("PROTONMAIL_IMAP_HOST"), "::1")

    def test_invalid_host_or_port_fails_before_credentials(self):
        for values in [{"PROTONMAIL_IMAP_HOST": ""}, {"PROTONMAIL_IMAP_HOST": "::1%lo0"},
                       *[{"PROTONMAIL_IMAP_PORT": value} for value in ["", "invalid", "0", "-1", "65536"]]]:
            with self.subTest(values=values), patch.dict(os.environ, values), \
                 patch.object(tool, "credentials") as creds, patch.object(tool.imaplib, "IMAP4") as factory:
                with self.assertRaises(SystemExit):
                    tool.connect_imap(arguments())
                creds.assert_not_called()
                factory.assert_not_called()

    def test_imap_tls_precedes_credentials_and_login_even_with_legacy_flags(self):
        events = []
        args = arguments()
        args.imap_host = "receiver.example.invalid"
        args.imap_port = 9999
        args.no_starttls = True
        client = Mock()
        client.starttls.side_effect = lambda **kw: events.append("starttls")
        client.login.side_effect = lambda *a: events.append("login")
        with patch.object(tool.socket, "getaddrinfo", return_value=[resolved("127.0.0.1")]), \
             patch.object(tool.imaplib, "IMAP4", return_value=client) as factory, \
             patch.object(tool, "credentials", side_effect=lambda a: (events.append("credentials") or ("dummy-user", "dummy-password"))):
            self.assertIs(tool.connect_imap(args), client)
        self.assertEqual(events, ["starttls", "credentials", "login"])
        factory.assert_called_once_with("127.0.0.1", 1143)
        context = client.starttls.call_args.kwargs["ssl_context"]
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)

    def test_smtp_authentication_order_and_sender_username(self):
        events = []
        client = Mock()
        client.ehlo.side_effect = lambda: events.append("ehlo")
        client.starttls.side_effect = lambda **kw: events.append("starttls")
        client.login.side_effect = lambda *a: events.append("login")
        with patch.object(tool.socket, "getaddrinfo", return_value=[resolved("::1", 1025)]), \
             patch.object(tool.smtplib, "SMTP", return_value=client) as factory, \
             patch.object(tool, "credentials", side_effect=lambda a: (events.append("credentials") or ("dummy-user", "dummy-password"))):
            self.assertEqual(tool.connect_smtp(arguments()), (client, "dummy-user"))
        self.assertEqual(events, ["ehlo", "starttls", "ehlo", "credentials", "login"])
        factory.assert_called_once_with("::1", 1025, timeout=30)

    def test_tls_failure_or_missing_starttls_never_reads_credentials_or_authenticates(self):
        for connector, module, name, close in [(tool.connect_imap, tool.imaplib, "IMAP4", "shutdown"),
                                               (tool.connect_smtp, tool.smtplib, "SMTP", "close")]:
            for failure in [ssl.SSLError("handshake failed"), smtplib.SMTPNotSupportedError("no STARTTLS")]:
                with self.subTest(connector=connector.__name__, failure=failure), \
                     patch.object(tool.socket, "getaddrinfo", return_value=[resolved("127.0.0.1")]), \
                     patch.object(module, name) as factory, patch.object(tool, "credentials") as creds:
                    factory.return_value.starttls.side_effect = failure
                    with self.assertRaises(type(failure)):
                        connector(arguments())
                    creds.assert_not_called()
                    factory.return_value.login.assert_not_called()
                    getattr(factory.return_value, close).assert_called_once_with()

    def test_tls_context_never_unverifies_remote_literal(self):
        with self.assertRaises(SystemExit):
            tool.tls_context("203.0.113.1", False)
        self.assertEqual(tool.tls_context("127.0.0.2", False).verify_mode, ssl.CERT_NONE)
        context = tool.tls_context("127.0.0.1", True)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_remote_host_rejected_even_with_verified_tls(self):
        args = arguments()
        args.local_bridge_tls = False
        with patch.dict(os.environ, {"PROTONMAIL_IMAP_HOST": "203.0.113.1"}), \
             patch.object(tool.socket, "getaddrinfo", return_value=[resolved("203.0.113.1")]), \
             patch.object(tool, "credentials") as creds:
            with self.assertRaises(SystemExit):
                tool.connect_imap(args)
            creds.assert_not_called()

    def test_send_preserves_from_precedence_recipients_body_and_quit(self):
        for explicit, configured, expected in [(None, None, "dummy-user"),
                                               (None, "configured@example.invalid", "configured@example.invalid"),
                                               ("explicit@example.invalid", "configured@example.invalid", "explicit@example.invalid")]:
            command = ["send", "--to", "to@example.invalid", "--cc", "cc@example.invalid", "--bcc", "bcc@example.invalid",
                       "--reply-to", "reply@example.invalid", "--subject", "dummy", "--body", "dummy body"]
            if explicit:
                command += ["--from", explicit]
            environment = {"PROTONMAIL_FROM": configured} if configured else {}
            client = Mock()
            with self.subTest(expected=expected), patch.dict(os.environ, environment), \
                 patch.object(tool, "connect_smtp", return_value=(client, "dummy-user")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tool.send(tool.build_parser().parse_args(command)), 0)
                message = client.send_message.call_args.args[0]
                self.assertEqual(str(message["From"]), expected)
                self.assertEqual(message["Reply-To"], "reply@example.invalid")
                self.assertEqual(message.get_content(), "dummy body\n")
                self.assertEqual(client.send_message.call_args.kwargs["to_addrs"],
                                 ["to@example.invalid", "cc@example.invalid", "bcc@example.invalid"])
                client.quit.assert_called_once_with()

    def test_all_imap_commands_share_connector(self):
        for command in [["list-folders"], ["search"], ["read", "--uid", "1"], ["move", "--uid", "1"]]:
            with self.subTest(command=command), patch.object(tool, "connect_imap", side_effect=RuntimeError("connector reached")) as connect:
                args = tool.build_parser().parse_args(command)
                with self.assertRaisesRegex(RuntimeError, "connector reached"):
                    args.func(args)
                connect.assert_called_once_with(args)


if __name__ == "__main__":
    unittest.main()
