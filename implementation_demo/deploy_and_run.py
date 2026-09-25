"""Deploy the touchscreen dashboard and driver bridge to the Raspberry Pi.

The launcher synchronises the runtime modules and dashboard, then starts the
bridge on the Pi. Authentication uses the SSH agent, normal SSH keys, or an
explicit identity file; credentials are never stored in this source file.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import shlex
import sys
import time

import paramiko


DEFAULT_HOST = "169.254.251.153"
DEFAULT_USER = "multiflo-display"
DEFAULT_REMOTE = "/home/multiflo-display/multiflo-app"
DEFAULT_HTTP_PORT = 8000
DEFAULT_SERIAL_PORT = "/dev/serial/by-id/usb-BTI_MultiFlo_14071419-if00-port0"
SERVICE_NAME = "multiflo-dashboard.service"

REPO = Path(__file__).resolve().parent.parent
CORE_DIR = REPO / "src" / "multiflo"
DEMO_DIR = Path(__file__).resolve().parent
CORE_FILES = (
    "__init__.py",
    "errors.py",
    "models.py",
    "codec.py",
    "transport.py",
    "driver.py",
    "runner.py",
    "logs.py",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("MULTIFLO_PI_HOST", DEFAULT_HOST))
    parser.add_argument("--user", default=os.getenv("MULTIFLO_PI_USER", DEFAULT_USER))
    parser.add_argument(
        "--remote",
        default=os.getenv("MULTIFLO_PI_REMOTE", DEFAULT_REMOTE),
        help="absolute deployment directory on the Raspberry Pi",
    )
    parser.add_argument(
        "--identity-file",
        default=os.getenv("MULTIFLO_SSH_KEY"),
        help="SSH private key; otherwise use the agent and standard key files",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=int(os.getenv("MULTIFLO_HTTP_PORT", str(DEFAULT_HTTP_PORT))),
    )
    parser.add_argument(
        "--serial-port",
        default=os.getenv("MULTIFLO_SERIAL_PORT", DEFAULT_SERIAL_PORT),
        help="stable serial-device path configured in the managed service",
    )
    parser.add_argument(
        "--accept-new-host-key",
        action="store_true",
        help="trust and remember the host key on first connection",
    )
    return parser


def _client(args: argparse.Namespace) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if args.accept_new_host_key:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

    connect: dict[str, object] = {
        "hostname": args.host,
        "username": args.user,
        "timeout": 15,
        "allow_agent": True,
        "look_for_keys": True,
    }
    if args.identity_file:
        connect["key_filename"] = str(Path(args.identity_file).expanduser())
    password = os.getenv("MULTIFLO_SSH_PASSWORD")
    if password:
        connect["password"] = password
    client.connect(**connect)
    return client


def _run(
    client: paramiko.SSHClient,
    command: str,
    timeout: int = 60,
) -> tuple[int, str, str]:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    output = stdout.read().decode("utf-8", "replace")
    error = stderr.read().decode("utf-8", "replace")
    return stdout.channel.recv_exit_status(), output, error


def _mkdirs(sftp: paramiko.SFTPClient, path: PurePosixPath) -> None:
    current = PurePosixPath("/")
    for part in path.parts[1:]:
        current /= part
        try:
            sftp.stat(str(current))
        except IOError:
            sftp.mkdir(str(current))


def _upload(client: paramiko.SSHClient, remote: PurePosixPath) -> None:
    sftp = client.open_sftp()
    try:
        _mkdirs(sftp, remote / "multiflo")
        _mkdirs(sftp, remote / "demo")
        for filename in CORE_FILES:
            source = CORE_DIR / filename
            if not source.is_file():
                raise FileNotFoundError(f"driver module is missing: {source}")
            sftp.put(str(source), str(remote / "multiflo" / filename))
        for filename in (
            "bridge_server.py",
            "widget.html",
            "kiosk.sh",
            "multiflo-dashboard.service",
            "multiflo-dashboard-shutdown.sudoers",
        ):
            sftp.put(str(DEMO_DIR / filename), str(remote / "demo" / filename))
    finally:
        sftp.close()


def _service_check_command(
    remote: PurePosixPath,
    http_port: int,
    serial_port: str = DEFAULT_SERIAL_PORT,
) -> str:
    service_q = shlex.quote(SERVICE_NAME)
    bridge_q = shlex.quote(str(remote / "demo" / "bridge_server.py"))
    listen_q = shlex.quote(f"--host 127.0.0.1 --http-port {http_port}")
    serial_q = shlex.quote(f"--serial-port {serial_port}")
    return (
        f"systemctl --user cat {service_q} >/dev/null && "
        f"systemctl --user cat {service_q} | grep -F -- {bridge_q} >/dev/null && "
        f"systemctl --user cat {service_q} | grep -F -- {listen_q} >/dev/null && "
        f"systemctl --user cat {service_q} | grep -F -- {serial_q} >/dev/null"
    )


def _service_restart_command() -> str:
    service_q = shlex.quote(SERVICE_NAME)
    return (
        "systemctl --user daemon-reload && "
        f"systemctl --user restart {service_q} && "
        f"systemctl --user is-active --quiet {service_q}"
    )


def _service_install_command(remote: PurePosixPath, user: str) -> str:
    staged_q = shlex.quote(str(remote / "demo" / "multiflo-dashboard.service"))
    target_q = shlex.quote(
        str(PurePosixPath("/home") / user / ".config/systemd/user" / SERVICE_NAME)
    )
    return (
        f"systemd-analyze --user verify {staged_q} && "
        f"install -D -m 0644 {staged_q} {target_q} && "
        "systemctl --user daemon-reload"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.remote.startswith("/"):
        raise SystemExit("--remote must be an absolute POSIX path")

    remote = PurePosixPath(args.remote)
    client = _client(args)
    try:
        _upload(client, remote)
        print("uploaded driver runtime + touchscreen dashboard + managed service")

        runtime = remote / ".venv" / "bin" / "python"
        check = f"test -x {shlex.quote(str(runtime))}"
        rc, _output, _error = _run(client, check, timeout=10)
        if rc:
            print(
                f"Pi runtime is missing at {runtime}. "
                "Create the virtual environment and install pydantic + pyserial first.",
                file=sys.stderr,
            )
            return 2

        rc, output, error = _run(
            client,
            _service_install_command(remote, args.user),
            timeout=20,
        )
        if rc:
            print(error.strip() or output.strip(), file=sys.stderr)
            return 1

        rc, _output, _error = _run(
            client,
            _service_check_command(remote, args.http_port, args.serial_port),
            timeout=10,
        )
        if rc:
            print(
                f"installed {SERVICE_NAME} does not match {remote}, loopback port "
                f"{args.http_port}, and serial path {args.serial_port}.",
                file=sys.stderr,
            )
            return 2

        rc, output, error = _run(
            client,
            _service_restart_command(),
            timeout=20,
        )
        if rc:
            print(error.strip() or output.strip(), file=sys.stderr)
            return 1

        for _ in range(20):
            time.sleep(0.5)
            rc, output, _error = _run(
                client,
                f"curl -fsS --max-time 2 http://127.0.0.1:{args.http_port}/v1/health",
                timeout=10,
            )
            if not rc:
                print(f"dashboard bridge ready: {output.strip()}")
                break
        else:
            _rc, output, _error = _run(
                client,
                f"journalctl --user -u {shlex.quote(SERVICE_NAME)} -n 20 --no-pager",
                timeout=10,
            )
            print("bridge did not become ready; journal tail:\n" + output)
            return 1
    finally:
        client.close()

    print(f"\nTouchscreen URL:  http://127.0.0.1:{args.http_port}/")
    print(
        "Laptop tunnel:   "
        f"ssh -L {args.http_port}:127.0.0.1:{args.http_port} "
        f"{args.user}@{args.host}"
    )
    print(f"Laptop URL:       http://127.0.0.1:{args.http_port}/ (after tunnel)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
