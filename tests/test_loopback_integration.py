"""Real CLI/stdlib clients against dummy loopback IMAP/SMTP servers only."""
import base64
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DummyServer:
    def __init__(self, protocol, tls_context, offer_tls=True, host="127.0.0.1"):
        self.protocol = protocol
        self.tls_context = tls_context
        self.offer_tls = offer_tls
        self.events = []
        self.error = None
        self.listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
        self.listener.bind((host, 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(1)
        self.listener.settimeout(10)
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.thread.join(timeout=12)
        self.listener.close()
        if self.thread.is_alive():
            raise AssertionError("Dummy server failed to stop")
        if self.error:
            raise self.error

    def serve(self):
        connection = None
        stream = None
        try:
            connection, _ = self.listener.accept()
            connection.settimeout(5)
            stream = connection.makefile("rwb")
            greeting = b"* OK dummy IMAP\r\n" if self.protocol == "IMAP" else b"220 dummy SMTP\r\n"
            stream.write(greeting)
            stream.flush()
            encrypted = False
            while True:
                line = stream.readline()
                if not line:
                    break
                if self.protocol == "IMAP":
                    tag, command = line.split(None, 2)[:2]
                else:
                    tag, command = b"", line.split(None, 1)[0]
                command = command.upper()
                self.events.append((command.decode(), encrypted, line.decode().strip()))
                if command == b"STARTTLS":
                    if not self.offer_tls:
                        stream.write(tag + b" NO no TLS\r\n")
                        stream.flush()
                        continue
                    stream.write(tag + b" OK upgrade\r\n" if self.protocol == "IMAP" else b"220 upgrade\r\n")
                    stream.flush()
                    stream.close()
                    connection = self.tls_context.wrap_socket(connection, server_side=True)
                    stream = connection.makefile("rwb")
                    encrypted = True
                    self.events.append(("TLS", True, "handshake completed"))
                    continue
                if self.protocol == "IMAP":
                    if command == b"CAPABILITY":
                        capabilities = b"IMAP4rev1 STARTTLS" if self.offer_tls else b"IMAP4rev1"
                        stream.write(b"* CAPABILITY " + capabilities + b"\r\n" + tag + b" OK capability\r\n")
                    elif command == b"LOGIN":
                        if not encrypted:
                            raise AssertionError("IMAP credentials sent before TLS")
                        if b'dummy-user "dummy-password"' not in line:
                            raise AssertionError("Unexpected credentials")
                        stream.write(tag + b" OK login\r\n")
                    elif command == b"LIST":
                        stream.write(b'* LIST () "/" "INBOX"\r\n' + tag + b" OK list\r\n")
                    elif command == b"LOGOUT":
                        stream.write(b"* BYE done\r\n" + tag + b" OK logout\r\n")
                        stream.flush()
                        break
                    else:
                        stream.write(tag + b" BAD unsupported\r\n")
                else:
                    if command == b"EHLO":
                        if not encrypted and self.offer_tls:
                            stream.write(b"250-dummy\r\n250 STARTTLS\r\n")
                        else:
                            stream.write(b"250-dummy\r\n250 AUTH PLAIN\r\n")
                    elif command == b"AUTH":
                        if not encrypted:
                            raise AssertionError("SMTP credentials sent before TLS")
                        if base64.b64decode(line.split()[2]) != b"\x00dummy-user\x00dummy-password":
                            raise AssertionError("Unexpected credentials")
                        stream.write(b"235 authenticated\r\n")
                    elif command in {b"MAIL", b"RCPT"}:
                        stream.write(b"250 accepted\r\n")
                    elif command == b"DATA":
                        stream.write(b"354 send data\r\n")
                        stream.flush()
                        payload = []
                        while True:
                            body_line = stream.readline()
                            if body_line == b".\r\n":
                                break
                            if not body_line:
                                raise AssertionError("Truncated dummy message")
                            payload.append(body_line)
                        self.events.append(("MESSAGE", encrypted, b"".join(payload).decode()))
                        stream.write(b"250 queued dummy message\r\n")
                    elif command == b"QUIT":
                        stream.write(b"221 bye\r\n")
                        stream.flush()
                        break
                    else:
                        stream.write(b"500 unsupported\r\n")
                stream.flush()
        except Exception as error:
            self.error = error
        finally:
            if stream:
                stream.close()
            if connection:
                connection.close()


class LoopbackIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cert = str(Path(cls.directory.name) / "dummy-cert.pem")
        key = str(Path(cls.directory.name) / "dummy-key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=localhost"],
                       check=True, capture_output=True, timeout=20)
        cls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.context.load_cert_chain(cert, key)

    def run_cli(self, protocol, port, command):
        environment = {"PATH": os.environ.get("PATH", ""), "PROTONMAIL_CONFIG": "/dev/null",
                       "PROTONMAIL_USERNAME": "dummy-user", "PROTONMAIL_PASSWORD": "dummy-password",
                       f"PROTONMAIL_{protocol}_HOST": "localhost", f"PROTONMAIL_{protocol}_PORT": str(port)}
        return subprocess.run([sys.executable, str(ROOT / "scripts/protonmail_tool.py")] + command,
                              env=environment, capture_output=True, text=True, timeout=10)

    def test_actual_imap_cli_upgrades_before_dummy_login(self):
        with DummyServer("IMAP", self.context) as server:
            result = self.run_cli("IMAP", server.port, ["list-folders"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INBOX", result.stdout)
        commands = [event[0] for event in server.events]
        self.assertLess(commands.index("TLS"), commands.index("LOGIN"))
        self.assertTrue(next(event[1] for event in server.events if event[0] == "LOGIN"))

    def test_actual_smtp_cli_upgrades_and_sends_only_dummy_message(self):
        with DummyServer("SMTP", self.context) as server:
            result = self.run_cli("SMTP", server.port, ["send", "--to", "nobody@example.invalid",
                                                      "--subject", "dummy", "--body", "dummy body"])
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [event[0] for event in server.events]
        self.assertEqual(commands[:5], ["EHLO", "STARTTLS", "TLS", "EHLO", "AUTH"])
        payload = next(event[2] for event in server.events if event[0] == "MESSAGE")
        self.assertIn("From: dummy-user", payload)
        self.assertIn("dummy body", payload)
        self.assertTrue(next(event[1] for event in server.events if event[0] == "AUTH"))

    def test_actual_clients_refuse_plaintext_servers(self):
        for protocol, command in [("IMAP", ["list-folders"]),
                                   ("SMTP", ["send", "--to", "nobody@example.invalid", "--subject", "dummy", "--body", "dummy"])]:
            with self.subTest(protocol=protocol), DummyServer(protocol, self.context, offer_tls=False) as server:
                result = self.run_cli(protocol, server.port, command)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(event[0] in {"LOGIN", "AUTH", "MESSAGE"} for event in server.events))

    def test_actual_localhost_clients_reach_ipv6_only_listener(self):
        for protocol, command in [("IMAP", ["list-folders"]),
                                   ("SMTP", ["send", "--to", "nobody@example.invalid", "--subject", "dummy", "--body", "dummy"])]:
            with self.subTest(protocol=protocol), DummyServer(protocol, self.context, host="::1") as server:
                result = self.run_cli(protocol, server.port, command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(any(event[0] == "TLS" for event in server.events))


if __name__ == "__main__":
    unittest.main()
