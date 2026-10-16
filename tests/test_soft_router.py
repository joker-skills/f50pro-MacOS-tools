import importlib.util
import json
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


class LifecycleTest(unittest.TestCase):
    def fake_service(self, root, action):
        script = (MODULE.parent / "service.sh").read_text()
        script = script.replace("ROOT=/data/adb/f50-soft-router", f"ROOT={root}")
        script = script.replace("/proc/", f"{root}/proc/")
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
            self.assertEqual(result.returncode, 23)
            self.assertTrue((root / "active").exists())

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

    def test_service_has_no_firmware_network_or_autostart_mutations(self):
        script = (MODULE.parent / "service.sh").read_text()
        for forbidden in ("iptables", "ip rule", "ip route", "service.d", "spd_dump", "init_boot"):
            self.assertNotIn(forbidden, script)
        self.assertIn("--tun=userspace-networking", script)
        self.assertIn("/proc/$pid/exe", script)
        self.assertIn("print $22", script)


if __name__ == "__main__":
    unittest.main()
