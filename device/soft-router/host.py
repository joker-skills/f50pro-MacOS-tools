#!/usr/bin/env python3
"""Stage 1: loopback explicit proxy and userspace Tailscale; never flash firmware."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

ROOT = "/data/adb/f50-soft-router"
HERE = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configuration(path):
    source = json.loads(path.read_text())
    allowed = {"proxies", "proxy-groups", "rules", "mixed-port", "bind-address",
               "allow-lan", "mode", "log-level", "ipv6", "tun", "dns", "interface-name"}
    if not isinstance(source, dict) or set(source) - allowed:
        raise ValueError("Only explicit proxy configuration is supported")
    if not source.get("proxies") or not source.get("rules"):
        raise ValueError("At least one proxy and routing rule are required")
    if any(not rule.startswith("MATCH,") for rule in source["rules"]):
        raise ValueError("Stage 1 supports MATCH rules only")
    return {**source, "mixed-port": 17890, "bind-address": "127.0.0.1",
            "allow-lan": False, "mode": "rule", "log-level": "warning",
            "ipv6": False, "interface-name": "sipa_eth8",
            "tun": {"enable": False}, "dns": {"enable": False}}


class Device:
    def __init__(self, serial):
        self.serial = serial

    def shell(self, script, *, data=None):
        return subprocess.run(["adb", "-s", self.serial, "shell",
                               "su -c " + shlex.quote(script)], input=data, capture_output=True,
                              timeout=90)

    def require(self, script, *, data=None):
        result = self.shell(script, data=data)
        if result.returncode:
            # Device stderr may include subscription contents: do not print it.
            raise RuntimeError(f"Device operation failed (exit {result.returncode})")
        return result.stdout

    def put(self, name, data, executable=False):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
            raise ValueError("Invalid destination")
        self.require(f"umask 077; cat > {ROOT}/{name}; chmod "
                     f"{'700' if executable else '600'} {ROOT}/{name}", data=data)

    def service(self, action):
        script = (HERE / "service.sh").read_text()
        return self.shell(f"sh -c {shlex.quote(script)} -- {shlex.quote(action)}")


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(data, indent=2) + "\n")


def apply(args, device):
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_oid or ""):
        raise ValueError("A full source commit OID is required")
    assets = Path(args.assets_dir)
    pins = json.loads((HERE / "artifacts.json").read_text())
    actual = {name: digest(assets / name) for name in pins["sha256"]}
    if actual != pins["sha256"]:
        raise ValueError("Asset SHA256 does not match pinned official release")
    config = json.dumps(configuration(Path(args.private_config))).encode()
    state = Path(args.state_dir)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    if (state / "deployment.json").exists():
        raise ValueError("State directory already used; check or restore before a new trial")
    exists = device.require(f"if [ -e {ROOT} ]; then echo exists; else echo absent; fi").strip()
    if exists == b"exists":
        device.require(f"[ ! -L {ROOT} ] && [ \"$(cat {ROOT}/owner)\" = f50-soft-router-v1 ]")
        if device.service("check").returncode != 10:
            raise ValueError("Existing deployment is not verifiably stopped")
    elif exists != b"absent":
        raise RuntimeError("Unknown device directory state")
    record = {"serial": args.serial, "source_oid": args.source_oid,
              "sha256": actual, "config_sha256": hashlib.sha256(config).hexdigest(),
              "service_sha256": digest(HERE / "service.sh"),
              "baseline": "stopped", "directory_existed": exists == b"exists"}
    private_write(state / "deployment.json", record)
    device.require(f"umask 077; mkdir -p {ROOT}; chmod 700 {ROOT}")
    device.put("owner", b"f50-soft-router-v1\n")
    device.put("deployment.json", json.dumps(record).encode())
    device.put("service.sh", (HERE / "service.sh").read_bytes(), executable=True)
    for name in actual:
        device.put(name, (assets / name).read_bytes(), executable=True)
    device.put("config.json", config)
    device.require(f"{ROOT}/mihomo -t -d {ROOT} -f {ROOT}/config.json >/dev/null 2>&1")
    result = device.service("start")
    if result.returncode:
        device.service("stop")
        raise RuntimeError("Start incomplete; use check then restore, do not replay apply")
    return check(args, device)


def check(args, device):
    absent = device.shell(f"[ ! -e {ROOT} ]")
    if absent.returncode == 0:
        return 10
    result = device.service("check")
    if result.returncode:
        return result.returncode if result.returncode == 10 else 20
    record = json.loads(device.require(f"cat {ROOT}/deployment.json"))
    if getattr(args, "state_dir", None):
        local = json.loads((Path(args.state_dir) / "deployment.json").read_text())
        if record != local:
            return 20
    if args.source_oid and record["source_oid"] != args.source_oid:
        return 20
    pinned = json.loads((HERE / "artifacts.json").read_text())["sha256"]
    if record["sha256"] != pinned:
        return 20
    if record["service_sha256"] != digest(HERE / "service.sh"):
        return 20
    expected = {**pinned, "config.json": record["config_sha256"],
                "service.sh": record["service_sha256"]}
    for name, sha in expected.items():
        got = device.require(f"sha256sum {ROOT}/{shlex.quote(name)}").decode().split()[0]
        if got != sha:
            return 20
    response = device.shell(f"{ROOT}/tailscale --socket={ROOT}/tailscaled.sock status --json")
    status = json.loads(response.stdout)
    if status.get("BackendState") not in {"NeedsLogin", "Running", "NeedsMachineAuth", "Stopped"}:
        return 20
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["apply", "check", "restore", "restore-check", "login", "ts-status"])
    parser.add_argument("--serial", required=True)
    parser.add_argument("--source-oid")
    parser.add_argument("--assets-dir")
    parser.add_argument("--private-config")
    parser.add_argument("--state-dir")
    args = parser.parse_args()
    device = Device(args.serial)
    lock = None
    try:
        lock_path = Path(tempfile.gettempdir()) / ("f50-soft-router-" +
                    hashlib.sha256(args.serial.encode()).hexdigest()[:16] + ".lock")
        lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "apply":
            result = apply(args, device)
        elif args.command == "check":
            result = check(args, device)
        elif args.command in ("restore", "restore-check"):
            record = json.loads((Path(args.state_dir) / "deployment.json").read_text())
            if record["serial"] != args.serial or record["source_oid"] != args.source_oid:
                raise ValueError("Restore target differs from saved baseline")
            response = device.shell(f"cat {ROOT}/deployment.json")
            try:
                remote = json.loads(response.stdout)
            except ValueError:
                # An interrupted initial upload cannot have launched services yet.
                # Confirm directly, without executing any partial device script.
                if device.service("idle").returncode != 0:
                    raise RuntimeError("Incomplete manifest and non-idle deployment")
                print(json.dumps({"command": args.command, "exit": 0, "state": "idle-partial-upload"}))
                return 0
            if remote != record:
                raise ValueError("Refusing to restore a different deployment")
            if args.command == "restore":
                if device.service("stop").returncode:
                    raise RuntimeError("Stop incomplete; inspect before retry")
            result = 0 if check(args, device) == 10 else 20
        else:
            if check(args, device) != 0:
                raise RuntimeError("Deployment is not healthy")
            base = f"{ROOT}/tailscale --socket={ROOT}/tailscaled.sock "
            if args.command == "login":
                response = device.shell(base + "up --accept-dns=false --accept-routes=false --shields-up --timeout=20s")
                output = (response.stdout + response.stderr).decode()
                urls = re.findall(r"https://login\.tailscale\.com/[A-Za-z0-9/_-]+", output)
                if response.returncode and not urls:
                    raise RuntimeError("Login failed without authentication URL")
                print("\n".join(urls) if urls else "No login URL returned; inspect ts-status")
            else:
                status = json.loads(device.require(base + "status --json"))
                print(json.dumps({"BackendState": status.get("BackendState"),
                                  "TailscaleIPs": status.get("TailscaleIPs", [])}))
            result = 0
        print(json.dumps({"command": args.command, "exit": result}))
        return result
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired, TypeError, KeyError) as exc:
        print(f"soft-router: {type(exc).__name__}: operation incomplete; no automatic replay", file=sys.stderr)
        return 20
    finally:
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    sys.exit(main())
