#!/usr/bin/env python3
"""Software-only explicit/transparent LAN proxy and userspace Tailscale."""
import argparse
import fcntl
import hashlib
import importlib.util
import io
import tarfile
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = "/data/adb/f50-soft-router"
BOOT = "/data/adb/service.d/f50-soft-router.sh"
HERE = Path(__file__).resolve().parent
AUTH = ROOT + "/auth"
AUTH_INJECTOR_SHA = "5358e6397ac5983c767b2137d4dd3e1f20af70f401d93075c99c846c7f09fe2c"
# frida-compile 19.1.0 + frida-java-bridge 7.0.13, rebuilt from auth-hook.js.
AUTH_BUNDLE_SHA = "9b2de68a79ceef8fb12581a9adf458da67f2f911c93dd30dcc53a10838be857c"


class OperationError(ValueError, RuntimeError):
    """A diagnostic written here, never a device/config/OS exception payload."""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configuration(path, transparent=False):
    text = path.read_text()
    try:
        source = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError:
            raise OperationError("YAML input requires PyYAML on the host") from None
        try:
            source = yaml.safe_load(text)
        except yaml.YAMLError:
            raise OperationError("Invalid private YAML configuration") from None
    root = Path(os.environ.get("F50_PROXY_CONFIG_ROOT", str(
        Path(os.environ.get("JOKER_REPOS", str(Path.home() / "repos"))) / "proxy-config")))
    helper = root / "render/f50.py"
    if not helper.is_file():
        raise OperationError("Missing central F50 adapter: set F50_PROXY_CONFIG_ROOT to its checkout")
    spec = importlib.util.spec_from_file_location("f50_central_adapter", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.adapt(source, transparent=transparent)
    except ValueError:
        raise OperationError("Invalid private F50 configuration") from None


# Mihomo filenames differ in case from Clash Verge's Mac cache; normalize on Android.
GEO_FILES = (("GeoSite.dat", "geosite.dat"), ("GeoIP.dat", "geoip.dat"),
             ("Country.mmdb", "country.mmdb"), ("ASN.mmdb",))


def geo_assets(directory):
    if directory is None:
        return {}
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise OperationError("Geo assets must be an explicit directory")
    assets = {}
    entries = set(os.listdir(root))
    for names in GEO_FILES:
        candidates = [root / name for name in names if name in entries]
        if len(candidates) > 1:
            raise OperationError("Ambiguous Geo asset filenames")
        if candidates:
            path = candidates[0]
            if path.is_symlink() or not path.is_file():
                raise OperationError("Geo assets must be regular files inside the supplied directory")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                assets[names[0]] = stream.read()
    if not assets:
        raise OperationError("No supported Geo assets in supplied directory")
    return assets


class Device:
    def __init__(self, serial):
        self.serial = serial

    def shell(self, script, *, data=None):
        return subprocess.run(["adb", "-s", self.serial, "shell",
                               "/debug_ramdisk/su -c " + shlex.quote(script)], input=data, capture_output=True,
                              timeout=240)

    def require(self, script, *, data=None):
        result = self.shell(script, data=data)
        if result.returncode:
            # Device stderr may include subscription contents: do not print it.
            raise OperationError(f"Device operation failed (exit {result.returncode})")
        return result.stdout

    def put(self, name, data, executable=False):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
            raise OperationError("Invalid destination")
        self.require(f"umask 077; cat > {ROOT}/{name}; chmod "
                     f"{'700' if executable else '600'} {ROOT}/{name}", data=data)

    def service(self, action):
        script = (HERE / "service.sh").read_text()
        if action.startswith("updates-"):
            script = script.replace('# UPDATE_FUNCTIONS', (HERE / "updates.sh").read_text())
        return self.shell(f"/data/adb/magisk/busybox ash -c {shlex.quote(script)} -- {shlex.quote(action)}")


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(data, indent=2) + "\n")


def apply(args, device):
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_oid or ""):
        raise OperationError("A full source commit OID is required")
    assets = Path(args.assets_dir)
    pins = json.loads((HERE / "artifacts.json").read_text())
    actual = {name: digest(assets / name) for name in pins["sha256"]}
    if actual != pins["sha256"]:
        raise OperationError("Asset SHA256 does not match pinned official release")
    transparent = getattr(args, "transparent", False)
    autostart = getattr(args, "autostart", False)
    # Mihomo reads JSON as YAML; YAML rejects JSON surrogate-pair escapes for emoji.
    config = json.dumps(configuration(Path(args.private_config), transparent),
                        ensure_ascii=False, indent=2).encode()
    geo = geo_assets(getattr(args, "geo_assets_dir", None))
    state = Path(args.state_dir)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    if (state / "deployment.json").exists():
        raise OperationError("State directory already used; check or restore before a new trial")
    exists = device.require(f"if [ -e {ROOT} ]; then echo exists; else echo absent; fi").strip()
    if exists == b"exists":
        device.require(f"[ ! -L {ROOT} ] && [ \"$(cat {ROOT}/owner)\" = f50-soft-router-v1 ]")
        if device.service("check").returncode != 10:
            raise OperationError("Existing deployment is not verifiably stopped")
    elif exists != b"absent":
        raise OperationError("Unknown device directory state")
    device.require(f"[ ! -e {BOOT} ] && [ ! -L {BOOT} ]")
    record = {"serial": args.serial, "source_oid": args.source_oid,
              "sha256": actual, "config_sha256": hashlib.sha256(config).hexdigest(),
              "service_sha256": digest(HERE / "service.sh"),
              "scripts_sha256": {name: digest(HERE / name) for name in ("network.sh", "boot.sh")},
              "transparent": transparent, "autostart": autostart,
              "baseline": "stopped", "directory_existed": exists == b"exists"}
    private_write(state / "deployment.json", record)
    if exists == b"absent":
        device.require(f"umask 077; mkdir {ROOT}")
    device.require(f"chmod 700 {ROOT}")
    device.put("owner", b"f50-soft-router-v1\n")
    device.put("deployment.json", json.dumps(record).encode())
    device.put("service.sh", (HERE / "service.sh").read_bytes(), executable=True)
    for name in record["scripts_sha256"]:
        device.put(name, (HERE / name).read_bytes(), executable=True)
    device.put("mode", b"transparent\n" if transparent else b"explicit\n")
    for name in actual:
        device.put(name, (assets / name).read_bytes(), executable=True)
    for name, data in geo.items():
        device.put(name, data)
    device.put("config.json", config)
    result = device.service("start")
    if result.returncode:
        if result.returncode != 28:
            stop_deployment(device, record)
        raise OperationError("Start incomplete; use check then restore, do not replay apply")
    try:
        tailscale_status(device, wait=True)
        if autostart:
            device.require(f"set -e; [ -d {str(Path(BOOT).parent)} ]; [ ! -e {BOOT} ]; umask 077; "
                           f"cp {ROOT}/boot.sh {ROOT}/boot.install; chmod 700 {ROOT}/boot.install; "
                           f"touch {ROOT}/boot-enabled; mv {ROOT}/boot.install {BOOT}")
        if check(args, device) != 0:
            raise OperationError("Post-start verification failed")
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired, TypeError, KeyError):
        stop_deployment(device, record)
        raise
    return 0


def check(args, device):
    absent = device.shell(f"[ ! -e {ROOT} ]")
    if absent.returncode == 0:
        return 10
    result = device.service("check")
    if result.returncode:
        return result.returncode if result.returncode == 10 else 20
    record = json.loads(device.require(f"cat {ROOT}/deployment.json"))
    updates = device.shell(f"[ -e {ROOT}/updates.enabled ] || [ -e {ROOT}/updates.backup ]")
    updating = updates.returncode == 0
    if updating and updates_check(args, device):
        return 20
    if getattr(args, "state_dir", None):
        local_path = Path(args.state_dir) / ("updates.json" if updating else "deployment.json")
        local = json.loads(local_path.read_text())
        if updating:
            local = local["deployment"]
        if record != local:
            return 20
    if args.source_oid and record["source_oid"] != args.source_oid:
        return 20
    mode = device.require(f"cat {ROOT}/mode").decode().strip()
    if mode != ("transparent" if record["transparent"] else "explicit"):
        return 20
    pinned = json.loads((HERE / "artifacts.json").read_text())["sha256"]
    if record["sha256"] != pinned:
        return 20
    if record["service_sha256"] != digest(HERE / "service.sh"):
        return 20
    config_sha = record["config_sha256"]
    if updating:
        config_sha = device.require(f"sed -n 's/^config_sha256=//p' {ROOT}/update.status").decode().strip()
        if not re.fullmatch("[0-9a-f]{64}", config_sha):
            return 20
    expected = {**pinned, "config.json": config_sha,
                "service.sh": record["service_sha256"]}
    for name in (("network.sh", "boot.sh", "updates.sh") if updating else ("network.sh", "boot.sh")):
        if record["scripts_sha256"][name] != digest(HERE / name):
            return 20
        expected[name] = record["scripts_sha256"][name]
    for name, sha in expected.items():
        got = device.require(f"sha256sum {ROOT}/{shlex.quote(name)}").decode().split()[0]
        if got != sha:
            return 20
    if record["autostart"]:
        boot_hash = device.require(f"[ -f {ROOT}/boot-enabled ] && sha256sum {BOOT}").decode().split()[0]
        if boot_hash != record["scripts_sha256"]["boot.sh"]:
            return 20
    tailscale_status(device)
    return 0


def recover_partial(device, record):
    if device.service("idle").returncode != 0:
        raise OperationError("Incomplete manifest and non-idle deployment")
    if device.shell(f"[ ! -e {ROOT} ] && [ ! -L {ROOT} ]").returncode == 0:
        return
    device.require(f"[ -d {ROOT} ] && [ ! -L {ROOT} ]")
    if device.shell(f'[ "$(cat {ROOT}/owner)" = f50-soft-router-v1 ]').returncode == 0:
        return
    if record["directory_existed"]:
        raise OperationError("Partial directory ownership cannot be confirmed")
    # Baseline was absent; rmdir only removes an empty upload directory, never data.
    device.require(f"rmdir {ROOT}")


def stop_deployment(device, record):
    try:
        device.require(f"rm -f {ROOT}/boot-enabled")
        if record.get("autostart") and device.shell(f"[ ! -e {BOOT} ] && [ ! -L {BOOT} ]").returncode != 0:
            actual = device.require(f"sha256sum {BOOT}").decode().split()[0]
            if actual != record["scripts_sha256"]["boot.sh"]:
                raise OperationError("Refusing to remove unknown autostart script")
            device.require(f"rm {BOOT}")
    finally:
        if device.service("stop").returncode:
            raise OperationError("Stop incomplete; inspect before retry")


def tailscale_status(device, wait=False):
    for attempt in range(20 if wait else 1):
        response = device.shell(f"{ROOT}/tailscale --socket={ROOT}/tailscaled.sock status --json")
        try:
            status = json.loads(response.stdout)
        except ValueError:
            if not wait:
                raise
            status = None
        state = status.get("BackendState") if isinstance(status, dict) else None
        if state in {"NeedsLogin", "Running", "NeedsMachineAuth", "Stopped"}:
            return status
        if wait and state in {"Starting", "NoState", None} and attempt < 19:
            time.sleep(1)
            continue
        raise OperationError("Tailscale has not reached a recognized ready state; inspect ts-status")


UPDATE_FILES = ("service.sh", "boot.sh", "deployment.json", "config.json", "runtime.json")


def update_args(args):
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_oid or "") or not args.state_dir:
        raise OperationError("Updates require a full source OID and private state directory")
    return Path(args.state_dir)


def updates_check(args, device):
    result = device.service("updates-check")
    if result.returncode == 10:
        return 10
    if result.returncode:
        return 20
    record = json.loads(device.require(f"cat {ROOT}/deployment.json"))
    if args.source_oid and record.get("source_oid") != args.source_oid:
        return 20
    for name in ("service.sh", "boot.sh", "updates.sh"):
        expected = record["service_sha256"] if name == "service.sh" else record["scripts_sha256"][name]
        if expected != digest(HERE / name):
            return 20
        if device.require(f"sha256sum {ROOT}/{name}").decode().split()[0] != expected:
            return 20
    if getattr(args, "state_dir", None):
        journal = Path(args.state_dir) / "updates.json"
        if not journal.is_file():
            raise OperationError("Enabled updates require their install state directory (updates.json)")
        saved = json.loads(journal.read_text())
        if saved["serial"] != args.serial or saved["deployment"] != record:
            return 20
    return 0


def stage_updates(device, files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(content))
    script = (f"set -e; umask 077; exec 9> {ROOT}/lifecycle.lock; "
              f"/data/adb/magisk/busybox flock -n 9; mkdir {ROOT}/updates.stage; "
              f"/data/adb/magisk/busybox tar -xf - -C {ROOT}/updates.stage")
    device.require("/data/adb/magisk/busybox ash -c " + shlex.quote(script), data=data.getvalue())


def updates_install(args, device):
    state = update_args(args)
    url_path = Path(args.subscription_file or "")
    if not url_path.is_file() or url_path.is_symlink() or url_path.stat().st_mode & 0o077:
        raise OperationError("Subscription file must be a private regular file")
    url = url_path.read_text().strip()
    # URL becomes a curl config value, never a shell/argv value.
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password or
            any(c.isspace() or c in '\\"' or ord(c) < 32 for c in url)):
        raise OperationError("Subscription must be one HTTPS URL without curl-config escapes")
    status = device.service("updates-check").returncode
    if status != 10:
        raise OperationError("Updates already enabled or state unknown; check/remove before installing")
    if device.service("check").returncode:
        raise OperationError("Existing running deployment must be healthy")
    device.require(f'[ ! -L {ROOT} ] && [ "$(stat -c %u:%a {ROOT})" = 0:700 ]')
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    journal = state / "updates.json"
    if journal.exists():
        previous = json.loads(journal.read_text())
        if (previous.get("phase") != "restored" or previous["serial"] != args.serial or
                previous["source_oid"] != args.source_oid):
            raise OperationError("State contains unresolved recovery material")
        verify_update_baseline(previous, state, device)
    originals = {}
    for name in UPDATE_FILES:
        originals[name] = device.require(f"cat {ROOT}/{name}")
    boot = device.shell(f"if [ -L {BOOT} ]; then exit 20; elif [ -f {BOOT} ]; then cat {BOOT}; "
                        f"elif [ -e {BOOT} ]; then exit 20; else exit 10; fi")
    if boot.returncode not in (0, 10):
        raise OperationError("Unknown boot entry")
    if boot.returncode == 0:
        originals["boot-entry"] = boot.stdout
    baseline = json.loads(originals["deployment.json"])
    expected_baseline = {"config.json": baseline["config_sha256"],
                         "service.sh": baseline["service_sha256"],
                         "boot.sh": baseline["scripts_sha256"]["boot.sh"]}
    if any(hashlib.sha256(originals[n]).hexdigest() != sha for n, sha in expected_baseline.items()):
        raise OperationError("Running baseline differs from its deployment manifest")
    pinned = json.loads((HERE / "artifacts.json").read_text())["sha256"]
    if baseline["sha256"] != pinned:
        raise OperationError("Running binary manifest differs from pinned releases")
    for name, sha in {**pinned, "network.sh": baseline["scripts_sha256"]["network.sh"]}.items():
        if device.require(f"sha256sum {ROOT}/{name}").decode().split()[0] != sha:
            raise OperationError("Running binary or network script digest differs")
    if "boot-entry" in originals and hashlib.sha256(originals["boot-entry"]).hexdigest() != expected_baseline["boot.sh"]:
        raise OperationError("Boot entry differs from the baseline script")
    if baseline["autostart"]:
        if "boot-entry" not in originals:
            raise OperationError("Baseline autostart entry is missing")
        device.require(f"[ -f {ROOT}/boot-enabled ]")
    config = json.loads(originals["config.json"])
    if config.get("external-controller-unix") or any(config.get(k) for k in
            ("external-controller", "external-controller-tls", "external-ui")):
        raise OperationError("Baseline has an unexpected controller")
    config["external-controller-unix"] = ROOT + "/controller.sock"
    identity = device.require(f"cat {ROOT}/mihomo.pid {ROOT}/tailscaled.pid")
    record = {**baseline, "source_oid": args.source_oid,
              "service_sha256": digest(HERE / "service.sh"),
              "scripts_sha256": {**baseline["scripts_sha256"],
                                 **{n: digest(HERE / n) for n in ("boot.sh", "updates.sh")}}}
    if baseline["scripts_sha256"]["network.sh"] != digest(HERE / "network.sh"):
        raise OperationError("Network script differs from this source")
    saved = {"serial": args.serial, "source_oid": args.source_oid, "phase": "prepared",
             "deployment": record, "identity": identity.decode(),
             "baseline_sha256": {n: hashlib.sha256(b).hexdigest() for n, b in originals.items()}}
    # Write recovery before touching any running file; fixed names never include URL/token.
    for name, content in originals.items():
        fd = os.open(state / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
    private_write(journal, saved)
    files = {n: (HERE / n).read_bytes() for n in ("service.sh", "boot.sh", "updates.sh")}
    files.update({"config.json": (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode(),
                  "deployment.json": json.dumps(record).encode(), "identity": identity,
                  "boot-present": b"yes\n" if "boot-entry" in originals else b"no\n",
                  "subscription.curl": ('url = "' + url + '"\n').encode(),
                  "baseline.sha": "".join(f"{sha}  {BOOT if n == 'boot-entry' else ROOT + '/' + n}\n"
                                          for n, sha in saved["baseline_sha256"].items()).encode()})
    # A failed/existing stage is unknown recovery material; do not overwrite or remove it.
    stage_updates(device, files)
    try:
        if device.service("updates-install").returncode:
            raise OperationError("Original-PID controller bootstrap failed")
        saved["phase"] = "installed"
        private_write(journal, saved)
        if check(args, device) or updates_check(args, device):
            raise OperationError("Installed updates failed verification")
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired, TypeError, KeyError):
        try:
            updates_remove(args, device)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired, TypeError, KeyError):
            raise OperationError("Install incomplete and recovery unverified; keep state and inspect") from None
        raise OperationError("Install failed; original running files and process identity restored") from None
    return 0


def verify_update_baseline(saved, state, device):
    for name, sha in saved["baseline_sha256"].items():
        path = BOOT if name == "boot-entry" else ROOT + "/" + name
        # runtime is derived from the original template and the current cellular interface.
        if digest(state / name) != sha or (name != "runtime.json" and
                device.require(f"sha256sum {path}").decode().split()[0] != sha):
            raise OperationError("Baseline file restoration could not be verified")
    if "boot-entry" not in saved["baseline_sha256"]:
        device.require(f"[ ! -e {BOOT} ] && [ ! -L {BOOT} ]")
    if device.require(f"cat {ROOT}/mihomo.pid {ROOT}/tailscaled.pid").decode() != saved["identity"]:
        raise OperationError("Original process identity changed")
    # Validate runtime/network normally, excluding update health while retaining its backup.
    if device.service("updates-restored-check").returncode:
        raise OperationError("Restored running baseline is unhealthy")


def updates_remove(args, device):
    state = update_args(args)
    saved = json.loads((state / "updates.json").read_text())
    if saved["serial"] != args.serial or saved["source_oid"] != args.source_oid:
        raise OperationError("Restore target differs from saved update baseline")
    remote = json.loads(device.require(f"cat {ROOT}/deployment.json"))
    baseline = json.loads((state / "deployment.json").read_text())
    if remote not in (saved["deployment"], baseline):
        raise OperationError("Refusing to remove a different deployment")
    staged = device.shell(f"cat {ROOT}/updates.stage/deployment.json")
    if staged.returncode == 0 and json.loads(staged.stdout) != saved["deployment"]:
        raise OperationError("Refusing to remove another staged update")
    restore_identity = device.require(f"cat {ROOT}/mihomo.pid {ROOT}/tailscaled.pid").decode()
    if device.service("updates-remove").returncode:
        raise OperationError("Running update removal incomplete; keep recovery material")
    verify_update_baseline({**saved, "identity": restore_identity}, state, device)
    if device.service("updates-finalize").returncode:
        raise OperationError("Restored baseline verified but update cleanup incomplete; keep state")
    if device.service("updates-check").returncode != 10:
        raise OperationError("Updates absence is not confirmed")
    saved["phase"] = "restored"
    saved["identity"] = restore_identity
    private_write(state / "updates.json", saved)
    return 0


def auth_service(device, action, *, data=None):
    script = (HERE / "auth-service.sh").read_text()
    return device.shell("/data/adb/magisk/busybox ash -c " + shlex.quote(script) +
                        " -- " + action, data=data)


def auth_state(args):
    if not args.state_dir or not re.fullmatch(r"[0-9a-f]{40}", args.source_oid or ""):
        raise OperationError("Auth install/remove/restore-check require --state-dir and a full --source-oid")
    state = Path(args.state_dir)
    if not state.is_absolute() or any(p.is_symlink() for p in [state, *state.parents]):
        raise OperationError("Auth state directory must be absolute without symlinks")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not state.is_dir() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o777 != 0o700:
        raise OperationError("Auth state directory must be owned by this user with mode 0700")
    return state


def auth_archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(content)
            item.mode = 0o700 if name in {"frida-inject", "auth-service.sh", "boot-entry"} else 0o600
            archive.addfile(item, io.BytesIO(content))
    return stream.getvalue()


def auth_backup(state, args, files):
    record = {"serial": args.serial, "source_oid": args.source_oid,
              "sha256": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}}
    journal = state / "auth.json"
    if journal.exists() or journal.is_symlink():
        previous, saved_files = auth_read_backup(state, args)
        if previous != record or saved_files != files:
            raise OperationError("Auth state contains a different backup; refusing overwrite")
        return record
    if any(state.iterdir()):
        raise OperationError("New auth state directory must be empty")
    for name, content in files.items():
        fd = os.open(state / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
    # Complete the local recovery set before any device mutation.
    private_write(journal, record)
    return record


def auth_read_backup(state, args):
    names = {"frida-inject", "auth-hook.bundle.js", "auth-service.sh", "boot-entry",
             "source_oid", "manifest", "manifest.sha256"}
    journal = state / "auth.json"
    if journal.is_symlink() or not journal.is_file() or journal.stat().st_mode & 0o077:
        raise OperationError("Missing or non-private auth backup journal")
    record = json.loads(journal.read_text())
    if (not isinstance(record, dict) or not isinstance(record.get("sha256"), dict) or
            record.get("serial") != args.serial or record.get("source_oid") != args.source_oid or
            set(record.get("sha256", {})) != names):
        raise OperationError("Auth backup belongs to a different identity or is incomplete")
    files = {}
    for name, sha in record["sha256"].items():
        path = state / name
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or digest(path) != sha:
            raise OperationError("Auth recovery file missing, unsafe or corrupt: " + name)
        files[name] = path.read_bytes()
    if (hashlib.sha256(files["manifest"]).hexdigest().encode() + b"\n" != files["manifest.sha256"] or
            files["source_oid"] != (args.source_oid + "\n").encode()):
        raise OperationError("Auth backup manifest identity differs")
    return record, files


def auth_install(args, device):
    state = auth_state(args)
    files = {}
    for name, supplied, expected in (("frida-inject", args.injector, AUTH_INJECTOR_SHA),
                                     ("auth-hook.bundle.js", args.bundle, AUTH_BUNDLE_SHA)):
        if not supplied or not Path(supplied).is_absolute():
            raise OperationError("Auth install requires absolute --injector and --bundle paths")
        path = Path(supplied)
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise OperationError("Auth asset differs from pinned release/source bundle: " + name)
        files[name] = path.read_bytes()
    files["auth-service.sh"] = (HERE / "auth-service.sh").read_bytes()
    service_sha = hashlib.sha256(files["auth-service.sh"]).hexdigest()
    recovery = ROOT + "/auth.recovery"
    files["boot-entry"] = (
        "#!/data/adb/magisk/busybox ash\numask 077\n"
        f'G={recovery}\nA={AUTH}\n'
        '[ -d "$A" ] && [ ! -L "$A" ] && [ "$(stat -c %u:%a "$A")" = 0:700 ] || exit 20\n'
        '[ -f "$A/enabled" ] && [ ! -L "$A/enabled" ] || exit 0\n'
        '[ -d "$G" ] && [ ! -L "$G" ] && [ "$(stat -c %u:%a "$G")" = 0:700 ] || exit 20\n'
        '[ -f "$G/auth-service.sh" ] && [ ! -L "$G/auth-service.sh" ] || exit 20\n'
        f'[ "$(sha256sum "$G/auth-service.sh" | cut -d " " -f 1)" = {service_sha} ] || exit 20\n'
        'if [ -e "$A/supervisor.log" ] || [ -L "$A/supervisor.log" ]; then\n'
        '  [ -f "$A/supervisor.log" ] && [ ! -L "$A/supervisor.log" ] && '
        '[ "$(stat -c %u:%a "$A/supervisor.log")" = 0:600 ] || exit 20\nfi\n'
        '/data/adb/magisk/busybox nohup /data/adb/magisk/busybox ash "$G/auth-service.sh" run '
        '</dev/null > "$A/supervisor.log" 2>&1 &\n'
    ).encode()
    files["source_oid"] = (args.source_oid + "\n").encode()
    files["manifest"] = "".join(hashlib.sha256(b).hexdigest() + "  " + n + "\n"
                                  for n, b in files.items()).encode()
    manifest_sha = hashlib.sha256(files["manifest"]).hexdigest()
    files["manifest.sha256"] = (manifest_sha + "\n").encode()
    auth_backup(state, args, files)
    # Auth can be used without an installed proxy. Never chmod an existing parent.
    device.require(f"umask 077; if [ ! -e {ROOT} ] && [ ! -L {ROOT} ]; then mkdir {ROOT}; fi; "
                   f'[ -d {ROOT} ] && [ ! -L {ROOT} ] && [ "$(stat -c %u:%a {ROOT})" = 0:700 ]')
    result = auth_service(device, "install " + manifest_sha, data=auth_archive(files))
    if result.returncode:
        raise OperationError("Auth install incomplete; keep backup and device recovery material")
    return auth_check(args, device)


def auth_check(args, device):
    result = auth_service(device, "check")
    try:
        status = json.loads(result.stdout)
        if not isinstance(status, dict) or not isinstance(status.get("probe"), dict):
            raise ValueError("Invalid auth status")
    except ValueError:
        status = {"source_oid": None, "state": "unknown", "target": None,
                  "supervisor": None, "injector": None, "files_healthy": False, "probe": {}}
    if result.returncode == 10 and status.get("state") == "uninstalled":
        print(json.dumps(status, separators=(",", ":")))
        return 10
    healthy = (result.returncode == 0 and status.get("state") == "healthy" and
               status.get("files_healthy") is True and status.get("probe", {}).get("passed") is True)
    status.setdefault("probe", {})["fresh"] = healthy
    if not healthy:
        status["probe"]["passed"] = False
        status["files_healthy"] = False
        if status.get("state") == "healthy":
            status["state"] = "unconfirmed"
    if args.source_oid and status.get("source_oid") != args.source_oid:
        healthy = False
        status["state"] = "source-mismatch"
    print(json.dumps(status, separators=(",", ":")))
    return 0 if healthy else 20


def auth_remove(args, device):
    state = auth_state(args)
    _, files = auth_read_backup(state, args)
    expected = hashlib.sha256(files["manifest"]).hexdigest()
    result = auth_service(device, "remove " + expected + " " + args.source_oid)
    if result.returncode:
        raise OperationError("Auth restore/detach unconfirmed; enabled revoked when possible; keep all material")
    return 0


def auth_restore_check(args, device):
    state = auth_state(args)
    _, files = auth_read_backup(state, args)
    expected = hashlib.sha256(files["manifest"]).hexdigest()
    result = auth_service(device, "restore-check " + expected + " " + args.source_oid)
    restored = result.returncode == 0
    status = {0: "uninstalled", 10: "deployed"}.get(result.returncode, "unconfirmed")
    print(json.dumps({"source_oid": args.source_oid, "state": status,
                      "restored": restored}, separators=(",", ":")))
    return result.returncode if result.returncode in (0, 10) else 20


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["apply", "check", "restore", "restore-check", "login", "ts-status",
                                                  "updates-install", "updates-check", "updates-remove",
                                                  "auth-install", "auth-check", "auth-remove", "auth-restore-check"])
    parser.add_argument("--serial", required=True)
    parser.add_argument("--source-oid")
    parser.add_argument("--assets-dir")
    parser.add_argument("--private-config", help="Private central Mihomo YAML or legacy JSON")
    parser.add_argument("--geo-assets-dir", help="Optional local directory of cached Mihomo Geo assets")
    parser.add_argument("--state-dir")
    parser.add_argument("--injector", help="Official Android ARM64 frida-inject 17.24.2 ELF")
    parser.add_argument("--bundle", help="Pinned compiled auth-hook.bundle.js")
    parser.add_argument("--subscription-file", help="Private HTTPS F50 JSON subscription URL file")
    parser.add_argument("--transparent", action="store_true")
    parser.add_argument("--autostart", action="store_true")
    args = parser.parse_args()
    device = Device(args.serial)
    lock = None
    try:
        lock_path = Path(tempfile.gettempdir()) / ("f50-soft-router-" +
                    hashlib.sha256(args.serial.encode()).hexdigest()[:16] + ".lock")
        lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command.startswith("auth-"):
            return {"auth-install": auth_install, "auth-check": auth_check,
                    "auth-remove": auth_remove, "auth-restore-check": auth_restore_check}[args.command](args, device)
        elif args.command == "apply":
            result = apply(args, device)
        elif args.command.startswith("updates-"):
            result = {"updates-install": updates_install, "updates-check": updates_check,
                      "updates-remove": updates_remove}[args.command](args, device)
        elif args.command == "check":
            result = check(args, device)
        elif args.command in ("restore", "restore-check"):
            record = json.loads((Path(args.state_dir) / "deployment.json").read_text())
            if record["serial"] != args.serial or record["source_oid"] != args.source_oid:
                raise OperationError("Restore target differs from saved baseline")
            response = device.shell(f"cat {ROOT}/deployment.json")
            try:
                remote = json.loads(response.stdout)
            except ValueError:
                # An interrupted initial upload cannot have launched services yet.
                # Confirm directly, without executing any partial device script.
                recover_partial(device, record)
                print(json.dumps({"command": args.command, "exit": 0, "state": "idle-partial-upload"}))
                return 0
            if remote != record:
                raise OperationError("Refusing to restore a different deployment")
            if args.command == "restore":
                stop_deployment(device, record)
            if record.get("autostart"):
                device.require(f"[ ! -e {BOOT} ] && [ ! -L {BOOT} ] && [ ! -e {ROOT}/boot-enabled ]")
            result = 0 if check(args, device) == 10 else 20
        else:
            if check(args, device) != 0:
                raise OperationError("Deployment is not healthy")
            base = f"{ROOT}/tailscale --socket={ROOT}/tailscaled.sock "
            if args.command == "login":
                response = device.shell(base + "up --accept-dns=false --accept-routes=false --shields-up --timeout=20s")
                output = (response.stdout + response.stderr).decode()
                urls = re.findall(r"https://login\.tailscale\.com/[A-Za-z0-9/_-]+", output)
                if response.returncode and not urls:
                    raise OperationError("Login failed without authentication URL")
                print("\n".join(urls) if urls else "No login URL returned; inspect ts-status")
            else:
                status = tailscale_status(device)
                print(json.dumps({"BackendState": status.get("BackendState"),
                                  "TailscaleIPs": status.get("TailscaleIPs", [])}))
            result = 0
        print(json.dumps({"command": args.command, "exit": result}))
        return result
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired, TypeError, KeyError) as exc:
        detail = str(exc) if isinstance(exc, OperationError) else type(exc).__name__
        print(f"soft-router: {detail}; operation incomplete; no automatic replay", file=sys.stderr)
        return 20
    finally:
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    sys.exit(main())
