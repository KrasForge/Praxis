"""One controller per store (ADR 0004) and client CA removal on reload."""

import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading

import pytest

from praxis.host.__main__ import main
from praxis.host.app import ControllerLocked, build_host, close_untrusted, controller_running
from praxis.host.auth import token_digest
from praxis.host.config import parse_host_config

OPS = "o" * 40


def config_data(tmp_path, server=None):
    data = {"data_dir": str(tmp_path / "data"),
            "clients": [{"id": "ops", "token_sha256": token_digest(OPS), "roles": ["admin"]}]}
    if server is not None:
        data["server"] = server
    return data


def test_second_controller_on_the_same_store_refuses_to_start(tmp_path):
    config = parse_host_config(config_data(tmp_path))
    first = build_host(config)
    with pytest.raises(ControllerLocked):
        build_host(config)
    assert controller_running(tmp_path / "data")
    first.close()
    assert not controller_running(tmp_path / "data")
    second = build_host(config)
    second.close()


def test_a_killed_controller_does_not_block_a_restart(tmp_path):
    (tmp_path / "data").mkdir()
    script = ("import os, signal, sys\nfrom pathlib import Path\n"
              "from praxis.host.app import acquire_controller\n"
              f"acquire_controller(Path({str(tmp_path / 'data')!r}))\n"
              "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
              "os.kill(os.getpid(), signal.SIGKILL)\n")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.stdout == "held\n" and result.returncode == -signal.SIGKILL
    assert (tmp_path / "data" / "controller.lock").exists()
    host = build_host(parse_host_config(config_data(tmp_path)))
    host.close()


def test_check_reports_and_retention_apply_refuses_a_running_controller(tmp_path, capsys):
    path = tmp_path / "host.toml"
    path.write_text(f'''data_dir = "{tmp_path / 'data'}"
[[clients]]
id = "ops"
token_sha256 = "{token_digest(OPS)}"
roles = ["admin"]
[retention]
workspace_days = 0
''')
    host = build_host(parse_host_config({**config_data(tmp_path), "retention": {"workspace_days": 0}}))
    assert main(["check", "--config", str(path)]) == 0
    assert "controller running" in capsys.readouterr().out
    assert main(["retention", "--config", str(path)]) == 0  # a report changes nothing
    assert main(["retention", "--config", str(path), "--apply"]) == 2
    assert "stop it before --apply" in capsys.readouterr().err
    host.close()
    assert main(["check", "--config", str(path)]) == 0
    assert "controller not running" in capsys.readouterr().out
    assert main(["retention", "--config", str(path), "--apply"]) == 0


def openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def mint(tmp_path):
    for ca in ("ca1", "ca2"):
        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", f"/CN={ca}",
                "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                "-keyout", f"{ca}.key", "-out", f"{ca}.pem", cwd=tmp_path)
    (tmp_path / "ext.cnf").write_text("basicConstraints=critical,CA:FALSE\n"
                                      "keyUsage=critical,digitalSignature,keyEncipherment\n"
                                      "extendedKeyUsage=serverAuth,clientAuth\nsubjectKeyIdentifier=hash\n"
                                      "authorityKeyIdentifier=keyid\nsubjectAltName=IP:127.0.0.1\n")
    for name, ca in (("server", "ca1"), ("alice", "ca1"), ("bob", "ca2")):
        openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={name}", "-keyout", f"{name}.key",
                "-out", f"{name}.csr", cwd=tmp_path)
        openssl("x509", "-req", "-in", f"{name}.csr", "-CA", f"{ca}.pem", "-CAkey", f"{ca}.key",
                "-CAcreateserial", "-days", "1", "-out", f"{name}.pem", "-extfile", "ext.cnf", cwd=tmp_path)
    (tmp_path / "both.pem").write_text((tmp_path / "ca1.pem").read_text() + (tmp_path / "ca2.pem").read_text())


def server_settings(tmp_path, ca):
    return {"host": "127.0.0.1", "tls_certfile": str(tmp_path / "server.pem"),
            "tls_keyfile": str(tmp_path / "server.key"), "tls_client_ca": str(tmp_path / ca)}


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI required to mint test certificates")
def test_removed_client_ca_is_refused_without_a_restart(tmp_path):
    mint(tmp_path)
    host = build_host(parse_host_config(config_data(tmp_path, server_settings(tmp_path, "both.pem"))))
    live = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)  # as uvicorn builds it at startup
    live.load_cert_chain(tmp_path / "server.pem", tmp_path / "server.key")
    live.load_verify_locations(tmp_path / "both.pem")
    live.verify_mode = ssl.CERT_REQUIRED
    host.server_ssl = live
    listener = socket.create_server(("127.0.0.1", 0))

    def serve_once():
        connection, _ = listener.accept()
        try:
            with live.wrap_socket(connection, server_side=True) as wrapped:
                wrapped.sendall(b"y")
                wrapped.recv(1)
        except (ssl.SSLError, OSError):
            pass

    def connect(name):
        thread = threading.Thread(target=serve_once)
        thread.start()
        client = ssl.create_default_context(cafile=str(tmp_path / "ca1.pem"))
        client.load_cert_chain(tmp_path / f"{name}.pem", tmp_path / f"{name}.key")
        try:
            with socket.create_connection(listener.getsockname()) as raw:
                with client.wrap_socket(raw, server_hostname="127.0.0.1") as wrapped:
                    wrapped.sendall(b"x")
                    return wrapped.recv(1) == b"y"
        except (ssl.SSLError, OSError):
            return False
        finally:
            thread.join()
    try:
        assert connect("alice") and connect("bob")
        changes = host.reload(parse_host_config(config_data(tmp_path, server_settings(tmp_path, "ca1.pem"))))
        assert "server certificate" in changes
        assert connect("alice")
        assert not connect("bob")
        # Adding the CA back is a reload too.
        host.reload(parse_host_config(config_data(tmp_path, server_settings(tmp_path, "both.pem"))))
        assert connect("bob")
    finally:
        listener.close()
        host.close()


class Transport:
    def __init__(self, issuer):
        self.issuer = issuer
        self.closed = False

    def get_extra_info(self, name):
        if self.issuer is None:
            return None

        class Connection:
            def getpeercert(inner):
                return {"issuer": ((("commonName", self.issuer),),)}
        return Connection()

    def close(self):
        self.closed = True


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI required to mint test certificates")
def test_open_connections_from_a_removed_ca_are_closed(tmp_path):
    mint(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_verify_locations(tmp_path / "ca1.pem")
    kept, removed, plain = Transport("ca1"), Transport("ca2"), Transport(None)
    assert close_untrusted([kept, removed, plain], context) == 1
    assert removed.closed and not kept.closed and not plain.closed
