import importlib.util
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "device/soft-router/host.py"
spec = importlib.util.spec_from_file_location("soft_router_host", MODULE)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class LocalShellDevice(host.Device):
    def shell(self, script, *, data=None):
        # macOS has shasum; directory guards and writes execute unchanged.
        script = script.replace("sha256sum ", "shasum -a 256 ")
        return subprocess.run(["sh", "-c", script], input=data, capture_output=True)

    def service(self, action):
        if action == "idle":
            script = (MODULE.parent / "service.sh").read_text().replace(
                "ROOT=/data/adb/f50-soft-router", f"ROOT={host.ROOT}")
            return self.shell(f"sh -c {host.shlex.quote(script)} -- idle")
        # Only Android process execution is replaced, by a controlled shell service.
        commands = {"start": "touch active", "stop": "rm -f active",
                    "check": "if [ -e active ]; then exit 0; else exit 10; fi"}
        return self.shell(f"cd {host.ROOT} && " + commands[action])


class ExplicitConfigTest(unittest.TestCase):
    def config(self, extra):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            path.write_text(json.dumps({"proxies": [{"name": "test"}],
                                        "rules": ["MATCH,test"], **extra}))
            return host.configuration(path)

    def test_forces_loopback_no_tun_no_dns(self):
        cfg = self.config({"bind-address": "*", "allow-lan": True,
                           "tun": {"enable": True}, "dns": {"enable": True}})
        self.assertEqual(cfg["bind-address"], "127.0.0.1")
        self.assertFalse(cfg["allow-lan"])
        self.assertFalse(cfg["tun"]["enable"])
        self.assertFalse(cfg["dns"]["enable"])
        self.assertEqual(cfg["mixed-port"], 17890)
        self.assertEqual(cfg["interface-name"], "sipa_eth8")

    def test_rejects_controller_and_external_providers(self):
        for key in ("external-controller", "proxy-providers", "listeners"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.config({key: {}})

    def test_rejects_rules_requiring_external_files(self):
        with self.assertRaises(ValueError):
            self.config({"rules": ["RULE-SET,private,test"]})

    def test_transparent_dns_is_internal_and_both_listeners_are_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"proxies": [{"name": "test"}], "rules": ["MATCH,test"]}))
            config = host.configuration(path, True)
        self.assertEqual(config["rules"][0], "DST-PORT,53,f50-dns")
        self.assertEqual(config["proxies"][-1], {"name": "f50-dns", "type": "dns"})
        self.assertEqual(config["dns"]["listen"], "127.0.0.1:0")
        self.assertTrue(config["dns"]["ipv6"])
        self.assertEqual({item["listen"] for item in config["listeners"]}, {"127.0.0.1", "::1"})
        self.assertFalse(config["tun"]["enable"])


class TransparentNetworkTest(unittest.TestCase):
    def test_real_shell_owns_only_scoped_dual_stack_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "owner").write_text("f50-soft-router-v1\n")
            kernel = root / "kernel"
            kernel.write_text("#!" + host.sys.executable + "\n" + r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['FAKE_KERNEL'])
statefile = root / 'kernel.json'
state = json.loads(statefile.read_text()) if statefile.exists() else {}
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with (root / 'commands').open('a') as log: log.write(name + ' ' + ' '.join(args) + '\n')
rc, out = 0, ''
if name == 'ip':
    family, kind, op = args[:3]
    key = family + kind
    if kind == 'addr':
        out = ' inet 192.168.0.1/24\n' if family == '-4' else ' inet6 2001:db8::1/64\n'
    elif op == 'add': state[key] = True
    elif op == 'del': state.pop(key, None)
    elif op == 'show' and state.get(key):
        out = '1789: from all fwmark 0x80000000/0x80000000 iif br0 lookup 17891\n' if kind == 'rule' else 'local default dev lo scope host\n'
else:
    args = args[2:]
    table = 'filter'
    if args[:1] == ['-t']: table, args = args[1], args[2:]
    op, chain = args[:2]
    rest = args[2:]
    if op == '-I': rest = rest[1:]
    key = name + table + chain
    rule = ' '.join(rest)
    if op in ('-S', '-C'):
        rc = 0 if key in state and (op == '-S' or rule in state[key]) else 1
    elif op == '-N': state[key] = []
    elif op in ('-A', '-I'): state.setdefault(key, []).append(rule)
    elif op == '-D': state[key].remove(rule)
    elif op == '-F': state[key] = []
    elif op == '-X': state.pop(key)
statefile.write_text(json.dumps(state))
sys.stdout.write(out)
sys.exit(rc)
''')
            kernel.chmod(0o700)
            for name in ("ip", "iptables", "ip6tables"):
                (root / name).symlink_to(kernel)
            script = (MODULE.parent / "network.sh").read_text().replace(
                "ROOT=/data/adb/f50-soft-router", f"ROOT={root}")
            script = script.replace("/system/bin/", str(root) + "/")
            poisoned = root / "wrong-tools"
            poisoned.mkdir()
            for name in ("ip", "iptables", "ip6tables"):
                tool = poisoned / name
                tool.write_text("#!/bin/sh\nexit 99\n")
                tool.chmod(0o700)
            env = {**os.environ, "PATH": str(poisoned) + os.pathsep + os.environ["PATH"], "FAKE_KERNEL": str(root)}
            for action in ("prepare", "enable", "check", "cleanup", "cleanup"):
                result = subprocess.run(["sh", "-c", script, "--", action], env=env, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
            lines = (root / "commands").read_text().splitlines()
            for name in ("iptables", "ip6tables"):
                rules = [line for line in lines if line.startswith(name) and " -A F50SR_TP " in line]
                if name == "ip6tables":
                    self.assertIn("-d fe80::/10 -j RETURN", rules.pop(0))
                self.assertTrue(all("--dport 53" in line for line in rules[:2]))
                self.assertTrue(any("-p tcp -j TPROXY" in line for line in rules))
                self.assertTrue(any("-p udp -j TPROXY" in line for line in rules))
            self.assertFalse(any(" -t nat " in line or " OUTPUT " in line or " FORWARD " in line for line in lines))
            additions = [line for line in lines if " rule add " in line]
            self.assertEqual(len(additions), 2)
            self.assertTrue(all("iif br0" in line for line in additions))
            self.assertFalse((root / "network-owned").exists())
            state = json.loads((root / "kernel.json").read_text())
            self.assertTrue(all(value == [] for value in state.values()))


class LifecycleTest(unittest.TestCase):
    def test_network_tools_are_pinned_outside_magisk_standalone(self):
        network = (MODULE.parent / "network.sh").read_text()
        self.assertNotRegex(network, r"(?<![/\w])ip\s+-")
        self.assertNotIn("xt=iptables", network)
        self.assertNotIn("xt=ip6tables", network)
        for executable in ("ip", "iptables", "ip6tables"):
            self.assertIn("/system/bin/" + executable, network)
        service = (MODULE.parent / "service.sh").read_text()
        self.assertEqual(service.count('/system/bin/sh "$ROOT/network.sh"'), 5)

    def test_boot_requires_enable_marker_and_runs_single_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, output in (("getprop", "1"), ("ip", " inet 192.168.0.1/24")):
                executable = root / name
                executable.write_text("#!/bin/sh\necho '" + output + "'\n")
                executable.chmod(0o700)
            (root / "service.sh").write_text('echo "$1" >> "' + str(root / "actions") + '"\n')
            script = (MODULE.parent / "boot.sh").read_text().replace(
                "ROOT=/data/adb/f50-soft-router", f"ROOT={root}")
            script = script.replace("/system/bin/", str(root) + "/")
            script = script.replace("/data/adb/magisk/busybox ash", "sh")
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            self.assertEqual(subprocess.run(["sh", "-c", script], env=env).returncode, 1)
            self.assertFalse((root / "actions").exists())
            (root / "boot-enabled").touch()
            self.assertEqual(subprocess.run(["sh", "-c", script], env=env).returncode, 0)
            self.assertEqual((root / "actions").read_text(), "boot\n")
            (root / "actions").unlink()
            with (root / "service.sh").open("a") as stream:
                stream.write("exit 28\n")
            self.assertEqual(subprocess.run(["sh", "-c", script], env=env).returncode, 28)
            self.assertEqual((root / "actions").read_text(), "boot\n")

    def test_apply_restore_reapply_real_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            assets = base / "assets"
            assets.mkdir()
            for name in ("mihomo", "tailscaled", "tailscale"):
                body = 'echo \'{"BackendState":"NeedsLogin"}\'\n' if name == "tailscale" else "exit 0\n"
                (assets / name).write_text("#!/bin/sh\n" + body)
            (assets / "artifacts.json").write_text(json.dumps({"sha256": {
                name: host.digest(assets / name) for name in ("mihomo", "tailscale", "tailscaled")}}))
            (assets / "service.sh").write_bytes((MODULE.parent / "service.sh").read_bytes())
            for name in ("network.sh", "boot.sh"):
                (assets / name).write_bytes((MODULE.parent / name).read_bytes())
            private = base / "private.json"
            private.write_text(json.dumps({"proxies": [{"name": "test"}], "rules": ["MATCH,test"]}))
            args = SimpleNamespace(serial="fake", source_oid="a" * 40,
                assets_dir=str(assets), private_config=str(private), state_dir=str(base / "trial1"), autostart=True)
            device = LocalShellDevice("fake")
            (base / "service.d").mkdir()
            boot = base / "service.d/f50-soft-router.sh"
            with patch.object(host, "ROOT", str(base / "device")), patch.object(host, "HERE", assets), \
                    patch.object(host, "BOOT", str(boot)):
                self.assertEqual(host.apply(args, device), 0)
                self.assertTrue(boot.exists())
                (base / "device/tailscale.state").write_text("private identity")
                argv = ["host.py", "restore", "--serial", "fake", "--source-oid", args.source_oid,
                        "--state-dir", args.state_dir]
                with patch.object(host.sys, "argv", argv), patch.object(host, "Device", return_value=device):
                    self.assertEqual(host.main(), 0)
                self.assertFalse(boot.exists())
                self.assertFalse((base / "device/boot-enabled").exists())
                self.assertEqual((base / "device/tailscale.state").read_text(), "private identity")
                self.assertEqual(host.check(args, device), 10)
                args.state_dir = str(base / "trial2")
                self.assertEqual(host.apply(args, device), 0)
                self.assertTrue(json.loads((base / "trial2/deployment.json").read_text())["directory_existed"])
                host.stop_deployment(device, json.loads((base / "trial2/deployment.json").read_text()))
                require = device.require
                def failed_install(script, *, data=None):
                    result = require(script, data=data)
                    if "mv " in script and "boot.install" in script:
                        raise host.OperationError("Autostart install verification failed")
                    return result
                for fault in ("readiness", "check", "install"):
                    args.state_dir = str(base / ("failed-" + fault))
                    with self.subTest(fault=fault), contextlib.ExitStack() as stack:
                        if fault == "readiness":
                            stack.enter_context(patch.object(host, "tailscale_status", side_effect=host.OperationError("Not ready")))
                        elif fault == "check":
                            stack.enter_context(patch.object(host, "check", return_value=20))
                        else:
                            stack.enter_context(patch.object(device, "require", side_effect=failed_install))
                        with self.assertRaises(host.OperationError):
                            host.apply(args, device)
                    self.assertFalse((base / "device/active").exists())
                    self.assertFalse((base / "device/boot-enabled").exists())
                    self.assertFalse(boot.exists())
                    self.assertEqual((base / "device/tailscale.state").read_text(), "private identity")

    def fake_service(self, root, action):
        script = (MODULE.parent / "service.sh").read_text()
        script = script.replace("ROOT=/data/adb/f50-soft-router", f"ROOT={root}")
        script = script.replace("/proc/", f"{root}/proc/")
        script = script.replace("/data/adb/magisk/busybox flock -n 9", "true")
        return subprocess.run(["sh", "-c", script, "--", action], capture_output=True)

    def test_recycled_pid_refuses_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "owner").write_text("f50-soft-router-v1\n")
            (root / "active").touch()
            (root / "mihomo.pid").write_text("99 124\n")
            proc = root / "proc/99"
            proc.mkdir(parents=True)
            (proc / "exe").symlink_to(root / "mihomo")
            (proc / "stat").write_text(" ".join(["0"] * 21 + ["123"]))
            result = self.fake_service(root, "stop")
            self.assertEqual(result.returncode, 27)
            self.assertTrue((root / "active").exists())

    def test_recycled_pid_with_unrelated_executable_clears_stale_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "owner").write_text("f50-soft-router-v1\n")
            (root / "active").touch()
            (root / "mihomo.pid").write_text("99 124\n")
            proc = root / "proc/99"
            proc.mkdir(parents=True)
            (proc / "exe").symlink_to("/usr/bin/true")
            (proc / "stat").write_text(" ".join(["0"] * 21 + ["123"]))
            self.assertEqual(self.fake_service(root, "stop").returncode, 0)
            self.assertFalse((root / "active").exists())
            self.assertFalse((root / "mihomo.pid").exists())

    def test_partial_upload_absent_and_empty_directory_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "device"
            with patch.object(host, "ROOT", str(root)):
                device = LocalShellDevice("fake")
                host.recover_partial(device, {"directory_existed": False})
                root.mkdir()
                host.recover_partial(device, {"directory_existed": False})
                self.assertFalse(root.exists())

    def test_partial_upload_preserves_unknown_content_and_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "device"
            root.mkdir()
            with patch.object(host, "ROOT", str(root)):
                device = LocalShellDevice("fake")
                with self.assertRaises(host.OperationError):
                    host.recover_partial(device, {"directory_existed": True})
                (root / "tailscale.state").write_text("private identity")
                with self.assertRaises(host.OperationError):
                    host.recover_partial(device, {"directory_existed": False})
                self.assertEqual((root / "tailscale.state").read_text(), "private identity")

    def test_ts_status_accepts_nonzero_valid_needs_login_json(self):
        device = SimpleNamespace(shell=lambda _: SimpleNamespace(returncode=1,
            stdout=b'{"BackendState":"NeedsLogin","TailscaleIPs":[]}'))
        output = io.StringIO()
        with patch.object(host, "Device", return_value=device), patch.object(host, "check", return_value=0), \
                patch.object(host.sys, "argv", ["host.py", "ts-status", "--serial", "fake"]), \
                contextlib.redirect_stdout(output):
            self.assertEqual(host.main(), 0)
        self.assertIn('"BackendState": "NeedsLogin"', output.getvalue())

    def test_startup_wait_reads_until_running_without_restarting(self):
        from unittest.mock import Mock
        device = SimpleNamespace(shell=Mock(side_effect=[
            SimpleNamespace(stdout=b'{"BackendState":"Starting"}'),
            SimpleNamespace(stdout=b'{"BackendState":"Running"}')]))
        with patch.object(host.time, "sleep") as sleep:
            self.assertEqual(host.tailscale_status(device, wait=True)["BackendState"], "Running")
            sleep.assert_called_once_with(1)
        self.assertEqual(device.shell.call_count, 2)
        self.assertTrue(all(" status --json" in call.args[0] for call in device.shell.call_args_list))

    def test_startup_wait_is_bounded(self):
        from unittest.mock import Mock
        for payload in (b'{"BackendState":"Starting"}', b''):
            device = SimpleNamespace(shell=Mock(return_value=SimpleNamespace(stdout=payload)))
            with self.subTest(payload=payload), patch.object(host.time, "sleep") as sleep, self.assertRaises(host.OperationError):
                host.tailscale_status(device, wait=True)
            self.assertEqual(device.shell.call_count, 20)
            self.assertEqual(sleep.call_count, 19)

    def test_startup_wait_accepts_initial_invalid_json(self):
        from unittest.mock import Mock
        device = SimpleNamespace(shell=Mock(side_effect=[SimpleNamespace(stdout=payload)
            for payload in (b'', b'not-json', b'{"BackendState":"Running"}')]))
        with patch.object(host.time, "sleep") as sleep:
            self.assertEqual(host.tailscale_status(device, wait=True)["BackendState"], "Running")
        self.assertEqual(sleep.call_count, 2)

    def test_errors_only_expose_our_safe_diagnostics(self):
        for error, expected in [(host.OperationError("Asset mismatch"), "Asset mismatch"),
                (json.JSONDecodeError("private-token", "private-config", 0), "JSONDecodeError")]:
            output = io.StringIO()
            with patch.object(host, "check", side_effect=error), \
                    patch.object(host.sys, "argv", ["host.py", "check", "--serial", "fake"]), \
                    contextlib.redirect_stderr(output):
                self.assertEqual(host.main(), 20)
            self.assertIn(expected, output.getvalue())
            self.assertNotIn("private", output.getvalue())

    def test_orphan_without_pid_file_is_not_reported_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "owner").write_text("f50-soft-router-v1\n")
            proc = root / "proc/99"
            proc.mkdir(parents=True)
            (proc / "exe").symlink_to(root / "mihomo")
            self.assertEqual(self.fake_service(root, "check").returncode, 24)

    def test_no_device_dir_means_not_applied(self):
        device = SimpleNamespace(shell=lambda _: SimpleNamespace(returncode=0))
        self.assertEqual(host.check(SimpleNamespace(source_oid=None), device), 10)

    def test_unknown_or_unreachable_is_not_not_applied(self):
        device = SimpleNamespace(shell=lambda _: SimpleNamespace(returncode=1),
                                 service=lambda _: SimpleNamespace(returncode=255))
        self.assertEqual(host.check(SimpleNamespace(source_oid=None), device), 20)

    def test_bad_asset_refused_before_device_access(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("mihomo", "tailscale", "tailscaled"):
                (Path(directory) / name).write_bytes(b"not the official binary")
            args = SimpleNamespace(source_oid="a" * 40, assets_dir=directory)
            with self.assertRaises(ValueError):
                host.apply(args, None)

    def test_binary_transfer_does_not_embed_secrets_in_argv(self):
        with patch.object(host.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout=b"")
            host.Device("serial").put("config.json", b"private-password")
            self.assertNotIn("private-password", str(run.call_args.args))
            self.assertEqual(run.call_args.kwargs["input"], b"private-password")

    def test_service_never_flashes_or_changes_system_network_directly(self):
        script = (MODULE.parent / "service.sh").read_text()
        for forbidden in ("iptables", "ip rule", "ip route", "spd_dump", "init_boot"):
            self.assertNotIn(forbidden, script)
        self.assertIn("--tun=userspace-networking", script)
        self.assertIn("/proc/$pid/exe", script)
        self.assertIn("print $22", script)
        self.assertEqual(script.count("exec 9>&-; trap '' HUP"), 2)
        self.assertTrue(script.startswith("#!/data/adb/magisk/busybox ash"))

    def test_service_uses_verified_busybox_ash_for_fd_lock(self):
        with patch.object(host.Device, "shell") as shell:
            host.Device("fake").service("check")
            self.assertTrue(shell.call_args.args[0].startswith("/data/adb/magisk/busybox ash -c "))
        self.assertEqual((MODULE.parent / "boot.sh").read_text().count(
            '/data/adb/magisk/busybox ash "$ROOT/service.sh"'), 2)


if __name__ == "__main__":
    unittest.main()
