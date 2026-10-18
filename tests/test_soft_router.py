import importlib.util
import contextlib
import hashlib
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


def setUpModule():
    global adapter_environment
    root = Path(os.environ.get("F50_PROXY_CONFIG_ROOT", str(
        Path(os.environ.get("JOKER_REPOS", str(Path.home() / "repos"))) / "proxy-config")))
    helper = root / "render/f50.py"
    expected = "faebc31fd41be0922f670fb8a85a86de3a9ebfc0580d11a5285ac7d85bf877ac"
    if not helper.is_file() or hashlib.sha256(helper.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Tests require render/f50.py from proxy-config bbf3c070; "
                           "set F50_PROXY_CONFIG_ROOT to that checkout (SHA256 " + expected + ")")
    adapter_environment = patch.dict(os.environ, {"F50_PROXY_CONFIG_ROOT": str(root.resolve())})
    adapter_environment.start()
    print("F50 test adapter: proxy-config bbf3c070, SHA256 " + expected)


def tearDownModule():
    adapter_environment.stop()


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
        self.assertEqual(cfg["interface-name"], "__F50_CELLULAR_INTERFACE__")

    def test_removes_source_ingress_and_controller(self):
        extra = {key: "source-setting" for key in ("port", "socks-port", "redir-port", "tproxy-port",
                 "listeners", "tunnels", "ss-config", "vmess-config", "tuic-server",
                 "external-controller", "external-controller-tls", "external-controller-unix",
                 "external-controller-pipe", "external-controller-cors", "external-ui", "external-ui-name",
                 "external-ui-url", "secret")}
        cfg = self.config(extra)
        self.assertFalse(set(extra) & set(cfg))

    def test_preserves_central_policy_for_yaml_and_json(self):
        import yaml
        source = {
            "proxies": [{"name": "test", "type": "ss", "server": "node.invalid", "port": 443}],
            "proxy-groups": [{"name": "auto", "type": "select", "use": ["airport"]}],
            "proxy-providers": {"airport": {"type": "http", "url": "https://subscription.invalid/feed",
                "path": "./providers/airport.yaml", "interval": 3600}},
            "rule-providers": {"cn": {"type": "http", "behavior": "domain",
                "url": "https://rules.invalid/cn", "path": "./rules/cn.yaml", "interval": 86400}},
            "rules": ["RULE-SET,cn,DIRECT", "GEOSITE,cn,DIRECT", "GEOIP,CN,DIRECT", "MATCH,auto"],
            "hosts": {"router.test": "192.168.0.1"},
            "sniffer": {"enable": True, "sniff": {"TLS": {"ports": [443]}}},
            "geodata-mode": True, "geo-auto-update": True,
            "dns": {"enable": True, "listen": "0.0.0.0:53", "ipv6": False, "enhanced-mode": "fake-ip",
                "nameserver": ["https://223.5.5.5/dns-query"], "default-nameserver": ["223.5.5.5"],
                "proxy-server-nameserver": ["119.29.29.29"],
                "nameserver-policy": {"geosite:cn": ["https://223.5.5.5/dns-query"]},
                "proxy-server-nameserver-policy": {"node.invalid": ["119.29.29.29"]},
                "respect-rules": False},
        }
        for encode in (json.dumps, yaml.safe_dump):
            for transparent in (False, True):
                with self.subTest(format=encode.__name__, transparent=transparent), tempfile.TemporaryDirectory() as d:
                    # Content decides the format, including subscription files without a suffix.
                    path = Path(d) / "private"
                    path.write_text(encode(source))
                    cfg = host.configuration(path, transparent)
                    for key in ("proxy-groups", "proxy-providers", "rule-providers", "hosts", "sniffer",
                                "geodata-mode", "geo-auto-update"):
                        self.assertEqual(cfg[key], source[key])
                    self.assertEqual(cfg["rules"][1:] if transparent else cfg["rules"], source["rules"])
                    self.assertEqual(cfg["proxies"][:-1] if transparent else cfg["proxies"], source["proxies"])
                    for key in ("nameserver", "default-nameserver", "proxy-server-nameserver",
                                "nameserver-policy", "proxy-server-nameserver-policy", "respect-rules"):
                        self.assertEqual(cfg["dns"][key], source["dns"][key])
                    if transparent:
                        self.assertEqual(cfg["dns"]["enhanced-mode"], "redir-host")
                        self.assertEqual(cfg["rules"][0], "DST-PORT,53,f50-dns")
                    self.assertEqual(cfg["dns"]["listen"], "127.0.0.1:0")
        # The same provider can supply every proxy without inline nodes.
        source.pop("proxies")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "provider.yaml"
            path.write_text(yaml.safe_dump(source))
            cfg = host.configuration(path, True)
            self.assertEqual(cfg["proxies"], [{"name": "f50-dns", "type": "dns"}])

    def test_invalid_input_and_reserved_dns_name_are_safe(self):
        for extra in ({"rules": "MATCH,test"}, {"rules": [1]}, {"proxies": ["invalid"]},
                      {"proxy-groups": ["invalid"]}, {"dns": []},
                      {"proxies": [{"name": "f50-dns"}]}, {"proxy-groups": [{"name": "f50-dns"}]}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "private"
                path.write_text(json.dumps({"proxies": [{"name": "test"}], "rules": ["MATCH,test"], **extra}))
                with self.assertRaises(host.OperationError):
                    host.configuration(path, True)
        for content in ("- private-token", "dns: [private-token", "!!python/object:private-token {}"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "private"
                path.write_text(content)
                with self.assertRaises(host.OperationError) as exc:
                    host.configuration(path, True)
                self.assertNotIn("private-token", str(exc.exception))

    def test_json_does_not_require_yaml_dependency(self):
        with patch.dict(host.sys.modules, {"yaml": None}):
            self.assertEqual(self.config({})["rules"], ["MATCH,test"])
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "private"
                path.write_text("rules: [MATCH,test]")
                with self.assertRaisesRegex(host.OperationError, "requires PyYAML"):
                    host.configuration(path)

    def test_transparent_dns_is_internal_and_both_listeners_are_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"proxies": [{"name": "test"}], "rules": ["MATCH,test"],
                                        "dns": {"enable": False}}))
            config = host.configuration(path, True)
        self.assertEqual(config["rules"][0], "DST-PORT,53,f50-dns")
        self.assertEqual(config["proxies"][-1], {"name": "f50-dns", "type": "dns"})
        self.assertEqual(config["dns"]["listen"], "127.0.0.1:0")
        self.assertTrue(config["dns"]["ipv6"])
        self.assertEqual(config["dns"]["nameserver"], ["https://1.1.1.1/dns-query"])
        self.assertEqual({item["listen"] for item in config["listeners"]}, {"127.0.0.1", "::1"})
        self.assertFalse(config["tun"]["enable"])


class GeoAssetsTest(unittest.TestCase):
    def test_upload_allowlist_normalizes_mac_cache_names(self):
        self.assertEqual(host.geo_assets(None), {})
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ("geosite.dat", "geoip.dat", "Country.mmdb", "ASN.mmdb", "config.yaml", "auth.json"):
                (root / name).write_bytes(name.encode())
            self.assertEqual(host.geo_assets(d), {"GeoSite.dat": b"geosite.dat", "GeoIP.dat": b"geoip.dat",
                "Country.mmdb": b"Country.mmdb", "ASN.mmdb": b"ASN.mmdb"})

    def test_rejects_symlink_escape_and_non_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / "cache"
            cache.mkdir()
            private = root / "private"
            private.write_text("private-token")
            for target in (private, root / "missing"):
                path = cache / "GeoSite.dat"
                path.symlink_to(target)
                with self.assertRaises(host.OperationError):
                    host.geo_assets(cache)
                path.unlink()
            (cache / "GeoSite.dat").mkdir()
            with self.assertRaises(host.OperationError):
                host.geo_assets(cache)
            with self.assertRaises(host.OperationError):
                host.geo_assets(private)
            with self.assertRaises(host.OperationError):
                host.geo_assets(root)


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


class CellularInterfaceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name, content in {"owner": "f50-soft-router-v1\n", "mode": "explicit\n",
                              "routes": "", "addresses": ""}.items():
            (self.root / name).write_text(content)
        self.script = (MODULE.parent / "service.sh").read_text().replace(
            "ROOT=/data/adb/f50-soft-router", f"ROOT={self.root}")
        self.script = self.script.replace("/proc/", f"{self.root}/proc/")
        self.script = self.script.replace("/system/bin/ip", str(self.root / "ip"))
        self.script = self.script.replace("/data/adb/magisk/busybox flock -n 9", "true")
        self.script = self.script.replace("sha256sum", "shasum -a 256")
        self.tool("ip", f'''[ ! -e "{self.root}/ip-fails" ] || exit 1
case "$*" in
    '-4 route show table all') cat "{self.root}/routes";;
    '-4 addr show') cat "{self.root}/addresses";;
    *) exit 99;;
esac
''')
        self.tool("mihomo", f'''printf '%s\\n' "$*" >> "{self.root}/preflight"
exit 1
''')
        private = self.root / "private.json"
        private.write_text(json.dumps({"proxies": [{"name": "🚀 节点",
            "interface-name": "__F50_CELLULAR_INTERFACE__"}], "rules": ["MATCH,🚀 节点"]}))
        self.config = host.configuration(private)
        (self.root / "config.json").write_text(json.dumps(self.config, ensure_ascii=False, indent=2))

    def tool(self, name, body):
        path = self.root / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o700)

    def link(self, name, up=True, ipv4=True):
        with (self.root / "addresses").open("a") as stream:
            stream.write(f"5: {name}: <BROADCAST,MULTICAST{',UP' if up else ''}> mtu 1400\n")
            if ipv4:
                stream.write("    inet 10.172.186.147/8 scope global " + name + "\n")
        with (self.root / "routes").open("a") as stream:
            stream.write(f"default dev {name} table {name} proto static scope link mtu 1400\n")

    def run_service(self, action):
        return subprocess.run(["sh", "-c", self.script, "--", action], capture_output=True)

    def running(self, interface):
        (self.root / "active").touch()
        for pid, name in ((91, "mihomo"), (92, "tailscaled")):
            proc = self.root / f"proc/{pid}"
            proc.mkdir(parents=True)
            (proc / "exe").symlink_to(self.root / name)
            (proc / "stat").write_text(" ".join(["0"] * 21 + ["123"]))
            (self.root / f"{name}.pid").write_text(f"{pid} 123\n")
        config = {**self.config, "interface-name": interface}
        (self.root / "runtime.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")

    def test_unique_eth0_and_eth8_deduplicated_and_read_only(self):
        for interface in ("sipa_eth0", "sipa_eth8"):
            with self.subTest(interface=interface):
                (self.root / "routes").write_text("default dev dummy0 table dummy0\n")
                (self.root / "addresses").write_text("")
                self.link(interface)
                with (self.root / "routes").open("a") as stream:
                    stream.write(f"default via 10.0.0.1 dev {interface} table extra\n")
                before = {p.name: p.read_bytes() for p in self.root.iterdir()}
                result = self.run_service("uplink")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.decode().strip(), interface)
                self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_no_candidate_down_missing_address_linkdown_and_ambiguity(self):
        for fault in ("none", "down", "no-ipv4", "linkdown", "ambiguous", "query"):
            with self.subTest(fault=fault):
                (self.root / "routes").write_text("")
                (self.root / "addresses").write_text("")
                if fault != "none":
                    self.link("sipa_eth0", up=fault != "down", ipv4=fault != "no-ipv4")
                if fault == "linkdown":
                    routes = self.root / "routes"
                    routes.write_text(routes.read_text().rstrip() + " linkdown\n")
                if fault == "ambiguous":
                    self.link("sipa_eth8")
                if fault == "query":
                    (self.root / "ip-fails").touch()
                self.assertNotEqual(self.run_service("uplink").returncode, 0)
                result = self.run_service("start")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "active").exists())
                self.assertFalse((self.root / "preflight").exists())
                self.assertFalse((self.root / "runtime.tmp").exists())

    def test_render_preflights_runtime_preserves_template_policy_and_unicode(self):
        self.link("sipa_eth0")
        template = (self.root / "config.json").read_bytes()
        # Force the real start to fail its preflight before starting any process or network rule.
        self.assertNotEqual(self.run_service("start").returncode, 0)
        runtime = self.root / "runtime.json"
        self.assertEqual(json.loads(runtime.read_text()), {**self.config, "interface-name": "sipa_eth0"})
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "config.json").read_bytes(), template)
        self.assertEqual((self.root / "preflight").read_text().strip(),
                         f"-t -d {self.root} -f {runtime}")
        self.assertFalse((self.root / "active").exists())
        self.assertFalse((self.root / "runtime.tmp").exists())
        (self.root / "ip-fails").touch()
        self.assertEqual(self.run_service("stop").returncode, 0)
        self.assertFalse(runtime.exists())

    def test_missing_or_duplicate_placeholder_refused_without_partial_runtime(self):
        self.link("sipa_eth0")
        for content in ('{"interface-name": "sipa_eth8"}',
                        '{\n  "interface-name": "__F50_CELLULAR_INTERFACE__",\n'
                        '  "interface-name": "__F50_CELLULAR_INTERFACE__"\n}'):
            with self.subTest(content=content):
                (self.root / "config.json").write_text(content)
                self.assertNotEqual(self.run_service("start").returncode, 0)
                self.assertFalse((self.root / "runtime.tmp").exists())
                self.assertFalse((self.root / "runtime.json").exists())
                self.assertFalse((self.root / "preflight").exists())

    def test_check_detects_tampering_missing_runtime_and_interface_change_without_writes(self):
        self.link("sipa_eth0")
        self.running("sipa_eth0")
        self.assertEqual(self.run_service("check").returncode, 0)
        runtime = self.root / "runtime.json"
        original = runtime.read_bytes()
        runtime.write_bytes(original.replace(b"MATCH,", b"DIRECT,"))
        tampered = runtime.read_bytes()
        self.assertNotEqual(self.run_service("check").returncode, 0)
        self.assertEqual(runtime.read_bytes(), tampered)
        runtime.unlink()
        self.assertNotEqual(self.run_service("check").returncode, 0)
        self.assertFalse(runtime.exists())
        runtime.write_bytes(original)
        (self.root / "routes").write_text("")
        (self.root / "addresses").write_text("")
        self.link("sipa_eth8")
        self.assertNotEqual(self.run_service("check").returncode, 0)
        self.assertEqual(runtime.read_bytes(), original)
        self.assertFalse((self.root / "runtime.tmp").exists())


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
            self.assertEqual((root / "actions").read_text(), "uplink\nboot\n")
            (root / "actions").unlink()
            with (root / "service.sh").open("a") as stream:
                stream.write('[ "$1" = uplink ] || exit 28\n')
            self.assertEqual(subprocess.run(["sh", "-c", script], env=env).returncode, 28)
            self.assertEqual((root / "actions").read_text(), "uplink\nboot\n")

    def test_boot_waits_for_shared_uplink_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "boot-enabled").touch()
            for name, body in {"getprop": "echo 1", "ip": "echo ' inet 192.168.0.1/24'",
                               "sleep": "exit 0"}.items():
                tool = root / name
                tool.write_text("#!/bin/sh\n" + body + "\n")
                tool.chmod(0o700)
            service = root / "service.sh"
            service.write_text(f'''echo "$1" >> "{root}/actions"
if [ "$1" = uplink ]; then
    [ -e "{root}/waited" ] || {{ touch "{root}/waited"; exit 29; }}
fi
''')
            script = (MODULE.parent / "boot.sh").read_text().replace(
                "ROOT=/data/adb/f50-soft-router", f"ROOT={root}")
            script = script.replace("/system/bin/", str(root) + "/")
            script = script.replace("/data/adb/magisk/busybox ash", "sh")
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            self.assertEqual(subprocess.run(["sh", "-c", script], env=env).returncode, 0)
            self.assertEqual((root / "actions").read_text(), "uplink\nuplink\nboot\n")
            (root / "actions").unlink()
            service.write_text(f'echo "$1" >> "{root}/actions"\nexit 29\n')
            self.assertEqual(subprocess.run(["sh", "-c", script], env=env).returncode, 1)
            self.assertEqual((root / "actions").read_text().splitlines(), ["uplink"] * 180)

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
            geo = base / "geo"
            geo.mkdir()
            (geo / "geosite.dat").write_bytes(b"cached geosite")
            (geo / "auth.json").write_bytes(b"private identity from Mac, never upload")
            private = base / "private.json"
            private.write_text(json.dumps({"proxies": [{"name": "🚀 节点"}], "rules": ["MATCH,🚀 节点"]}))
            args = SimpleNamespace(serial="fake", source_oid="a" * 40,
                assets_dir=str(assets), private_config=str(private), state_dir=str(base / "trial1"),
                geo_assets_dir=str(geo), autostart=True)
            device = LocalShellDevice("fake")
            (base / "service.d").mkdir()
            boot = base / "service.d/f50-soft-router.sh"
            with patch.object(host, "ROOT", str(base / "device")), patch.object(host, "HERE", assets), \
                    patch.object(host, "BOOT", str(boot)):
                self.assertEqual(host.apply(args, device), 0)
                self.assertTrue(boot.exists())
                deployed_config = (base / "device/config.json").read_text()
                self.assertIn("🚀 节点", deployed_config)
                self.assertNotIn("\\ud83d", deployed_config)
                self.assertEqual((base / "device/GeoSite.dat").read_bytes(), b"cached geosite")
                self.assertFalse((base / "device/auth.json").exists())
                for name in ("config.json", "GeoSite.dat"):
                    self.assertEqual((base / "device" / name).stat().st_mode & 0o777, 0o600)
                self.assertEqual((base / "device").stat().st_mode & 0o777, 0o700)
                running_args = SimpleNamespace(**{**vars(args), "state_dir": str(base / "running")})
                with self.assertRaisesRegex(host.OperationError, "verifiably stopped"):
                    host.apply(running_args, device)
                self.assertTrue((base / "device/active").exists())
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
                service = device.service
                for fault in ("start", "readiness", "check", "install"):
                    args.state_dir = str(base / ("failed-" + fault))
                    with self.subTest(fault=fault), contextlib.ExitStack() as stack:
                        if fault == "start":
                            stack.enter_context(patch.object(device, "service", side_effect=lambda action:
                                SimpleNamespace(returncode=29) if action == "start" else service(action)))
                        elif fault == "readiness":
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
            '/data/adb/magisk/busybox ash "$ROOT/service.sh"'), 3)


class UpdateServiceTest(unittest.TestCase):
    setUp = CellularInterfaceTest.setUp
    tool = CellularInterfaceTest.tool
    link = CellularInterfaceTest.link
    running = CellularInterfaceTest.running

    def prepare(self):
        self.link("sipa_eth0")
        self.running("sipa_eth0")
        self.boot = self.root / "boot-entry"
        self.boot.write_text("original boot\n")
        self.original = {n: (self.root / n).read_bytes() for n in ("config.json", "runtime.json")}
        for name in ("service.sh", "boot.sh", "deployment.json"):
            (self.root / name).write_text("original " + name)
        (self.root / "loaded.json").write_bytes(self.original["runtime.json"])
        self.helper = (MODULE.parent / "updates.sh").read_text().replace(
            "BOOT=/data/adb/service.d/f50-soft-router.sh", f"BOOT={self.boot}")
        self.helper = self.helper.replace("/proc/", f"{self.root}/proc/")
        self.helper = self.helper.replace("sha256sum", "shasum -a 256")
        self.helper = self.helper.replace("$(stat -c %u:%a subscription.curl)", "0:600")
        self.script = self.script.replace("# UPDATE_FUNCTIONS", self.helper + f'''
private_root() {{ return 0; }}
schedule() {{ echo schedule >> "{self.root}/schedules"; }}
unschedule() {{ echo unschedule >> "{self.root}/schedules"; }}
sleep() {{ :; }}
kill() {{
    echo "$*" >> "{self.root}/signals"
    [ ! -e "{self.root}/hup-fails" ] || return 1
    cp "{self.root}/runtime.json" "{self.root}/loaded.json"
}}
''')
        self.tool("mihomo", f'''exec {host.shlex.quote(host.sys.executable)} -c '
import json,sys
c=json.load(open(sys.argv[-1]))
assert not any("missing-target" in r for r in c["rules"])
' "$@"
''')
        self.tool("curl", "exec " + host.shlex.quote(host.sys.executable) + " " +
                  host.shlex.quote(str(self.root / "fake-curl.py")) + ' "$@"\n')
        (self.root / "fake-curl.py").write_text('''import json,sys,pathlib,shutil
root=pathlib.Path(__file__).parent
a=sys.argv[1:]
with (root/'curl-calls').open('a') as f: f.write(' '.join(a)+'\\n')
if '--config' in a:
 if (root/'download-fails').exists():
  print('HTTP 404: private-token',file=sys.stderr);sys.exit(22)
 shutil.copyfile(root/'next.json',root/a[a.index('--output')+1]);sys.exit(0)
if '-X' in a:
 shutil.copyfile(root/'runtime.json',root/'loaded.json')
 if (root/'reload-timeout').exists(): sys.exit(28)
 print('204',end='');sys.exit(0)
c=json.loads((root/'loaded.json').read_text())
if 'external-controller-unix' not in c: sys.exit(7)
endpoint=a[-1].split('/')[-1]
if endpoint=='rules':
 rows=[]
 for r in c['rules']:
  p=r.split(',');t=p[0].replace('-','');payload=p[1] if len(p)>2 else ''
  rows.append(dict(index=len(rows),type=t,payload=payload.lower() if t=='GEOIP' else payload,
                   proxy=p[2] if len(p)>2 else p[1],size=-1,
                   extra=dict(disabled=False,hitCount=0)))
 if (root/'stale-rules').exists(): rows=[]
 print(json.dumps(dict(rules=rows),ensure_ascii=False,separators=(',',':')))
elif endpoint=='group':
 names=['GLOBAL']+[g['name'] for g in c.get('proxy-groups',[])]
 if (root/'stale-groups').exists(): names.append('obsolete-group')
 print(json.dumps(dict(proxies=[dict(name=n) for n in names]),ensure_ascii=False,separators=(',',':')))
elif endpoint=='proxies':
 print(json.dumps(dict(proxies={g['name']:dict(type='Selector') for g in c.get('proxy-groups',[])}),ensure_ascii=False,separators=(',',':')))
else: print('{}')
''')
        self.env = {**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"]}
        stage = self.root / "updates.stage"
        stage.mkdir()
        for name in ("service.sh", "boot.sh", "updates.sh", "deployment.json"):
            (stage / name).write_text("candidate " + name)
        cfg = {**self.config, "external-controller-unix": host.ROOT + "/controller.sock"}
        (stage / "config.json").write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n')
        (stage / "subscription.curl").write_text('url = "https://private.invalid/token"\n')
        (stage / "boot-present").write_text("yes\n")
        (stage / "identity").write_text('91 123\n92 123\n')
        (stage / "baseline.sha").write_text(''.join(
            host.digest(self.root/n)+'  '+str(self.root/n)+'\n' for n in
            ("config.json", "runtime.json", "service.sh", "boot.sh", "deployment.json")))
        self.next_config(cfg)
        return cfg

    def run_service(self, action):
        return subprocess.run(["sh", "-c", self.script, "--", action], env=self.env, capture_output=True)

    def installed(self):
        cfg = self.prepare()
        result = self.run_service("updates-install")
        self.assertEqual(result.returncode,0,result.stderr.decode())
        # Helper sourcing is bypassed by the same inline functions on every invocation.
        return cfg

    def next_config(self, cfg):
        (self.root / "next.json").write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n')

    def status(self):
        return dict(line.split('=',1) for line in (self.root / "update.status").read_text().splitlines())

    def test_install_preflight_failure_preserves_running_files_and_never_signals(self):
        cfg = self.prepare()
        paths = [self.root / name for name in (*host.UPDATE_FILES, "mihomo.pid", "tailscaled.pid", "loaded.json")]
        paths.append(self.boot)
        def running_state():
            return {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_mode)
                    for p in paths}
        baseline = running_state()
        failures = {
            "404": None,
            "empty": b"",
            "truncated": b'{"rules": [',
            "oversize": b" " * (4194304 + 1),
            "changed-ingress": json.dumps({**cfg, "mixed-port": 1080}, indent=2).encode(),
            "invalid-core-config": json.dumps({**cfg, "rules": ["MATCH,missing-target"]}, indent=2).encode(),
        }
        for fault, content in failures.items():
            with self.subTest(fault=fault):
                marker = self.root / "download-fails"
                if content is None:
                    marker.touch()
                else:
                    (self.root / "next.json").write_bytes(content)
                result = self.run_service("updates-install")
                marker.unlink(missing_ok=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b"private-token", result.stdout + result.stderr)
                self.assertEqual(running_state(), baseline)
                for name in ("updates.backup", "updates.backup.new", "updates.enabled", "updates.pending",
                             "subscription.curl", "update.status", "signals", "schedules", "cron",
                             "updates.download", "updates.runtime", "updates.envelope", "updates.envelope.raw"):
                    self.assertFalse((self.root / name).exists(), name)
        calls = (self.root / "curl-calls").read_text()
        self.assertIn("--config updates.stage/subscription.curl", calls)
        self.assertIn("--proto =https --proto-redir =https", calls)
        self.assertIn("--proxy http://127.0.0.1:17890", calls)
        self.assertNotIn("-X PUT", calls)

    def test_install_validates_new_center_config_but_keeps_current_policy(self):
        cfg = self.prepare()
        cfg["rules"] = ["DOMAIN,preflight-only.invalid,DIRECT", *cfg["rules"]]
        cfg["proxy-groups"] = [{"name": "preflight-only", "type": "select", "proxies": ["DIRECT"]}]
        self.next_config(cfg)
        result = self.run_service("updates-install")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        template = json.loads((self.root / "config.json").read_text())
        runtime = json.loads((self.root / "loaded.json").read_text())
        for value in (template, runtime):
            self.assertEqual(value["rules"], self.config["rules"])
            self.assertNotIn("proxy-groups", value)
        self.assertEqual(self.status()["reload_count"], "0")
        self.assertEqual((self.root / "signals").read_text().splitlines(), ["-HUP 91"])
        self.assertEqual((self.root / "schedules").read_text().splitlines(), ["schedule"])
        self.assertFalse((self.root / "updates.download").exists())
        self.assertFalse((self.root / "updates.runtime").exists())

    def test_changed_unchanged_invalid_and_download_failures(self):
        cfg = self.installed()
        self.next_config(cfg)
        self.assertEqual(self.run_service("update").returncode,0)
        before = {n: ((self.root/n).stat().st_ino,(self.root/n).read_bytes()) for n in self.original}
        self.assertEqual(self.status()["result"],"unchanged")
        self.assertEqual(self.status()["reload_count"],"0")
        self.assertEqual(before,{n: ((self.root/n).stat().st_ino,(self.root/n).read_bytes()) for n in self.original})
        cfg['rules'].insert(0,'DOMAIN,new.invalid,DIRECT')
        cfg['proxy-groups']=[dict(name='new.invalid',type='select',proxies=['DIRECT'])]
        self.next_config(cfg)
        self.assertEqual(self.run_service("update").returncode,0)
        self.assertEqual(self.status()["result"],"changed")
        self.assertEqual(self.status()["reload_count"],"1")
        stable = {n:(self.root/n).read_bytes() for n in self.original}
        cfg['rules'][0]='DOMAIN,new.invalid,missing-target'
        self.next_config(cfg)
        self.assertNotEqual(self.run_service("update").returncode,0)
        self.assertEqual(self.status()["result"],"invalid")
        (self.root/'download-fails').touch()
        self.assertNotEqual(self.run_service("update").returncode,0)
        self.assertEqual(self.status()["result"],"download-failed")
        self.assertEqual(stable,{n:(self.root/n).read_bytes() for n in self.original})
        self.assertFalse((self.root/'updates.pending').exists())
        (self.root/'active').unlink()
        calls=(self.root/'curl-calls').read_bytes()
        self.assertNotEqual(self.run_service("update").returncode,0)
        self.assertEqual(calls,(self.root/'curl-calls').read_bytes())

    def test_rejects_ingress_format_and_duplicate_fields_before_reload(self):
        cfg = self.installed()
        baseline = {n:(self.root/n).read_bytes() for n in self.original}
        for changed in ({**cfg,'external-controller':'0.0.0.0:9090'},
                        {**cfg,'mixed-port':1080}, {**cfg,'tun':{'enable':True}},
                        {**cfg,'dns':{**cfg['dns'],'listen':'0.0.0.0:53'}},
                        {**cfg,'interface-name':'sipa_eth9'}):
            self.next_config(changed)
            self.assertNotEqual(self.run_service('update').returncode,0)
        for text in (json.dumps(cfg),json.dumps(cfg,indent=2).replace(
                '"mixed-port": 17890,','"mixed-port": 17890,\n  "mixed-port": 17890,')):
            (self.root/'next.json').write_text(text)
            self.assertNotEqual(self.run_service('update').returncode,0)
        self.assertEqual(baseline,{n:(self.root/n).read_bytes() for n in self.original})
        self.assertNotIn('-X PUT',(self.root/'curl-calls').read_text())

    def test_unknown_reload_blocks_next_attempt_and_removal_restores_baseline(self):
        cfg=self.installed()
        cfg['rules'].insert(0,'DOMAIN,new.invalid,DIRECT')
        self.next_config(cfg)
        (self.root/'reload-timeout').touch()
        self.assertNotEqual(self.run_service('update').returncode,0)
        self.assertTrue((self.root/'updates.pending').exists())
        self.assertEqual(self.status()['result'],'reload-unknown')
        self.assertEqual(self.status()['reload_count'],'0')
        calls=(self.root/'curl-calls').read_bytes()
        self.assertNotEqual(self.run_service('update').returncode,0)
        self.assertEqual(calls,(self.root/'curl-calls').read_bytes())
        (self.root/'reload-timeout').unlink()
        result=self.run_service('updates-remove')
        self.assertEqual(result.returncode,0,result.stderr.decode())
        self.assertEqual(self.original,{n:(self.root/n).read_bytes() for n in self.original})
        self.assertTrue((self.root/'updates.backup').exists())
        self.assertEqual(self.run_service('updates-check').returncode,20)
        self.assertEqual(self.run_service('updates-finalize').returncode,0)
        self.assertFalse((self.root/'updates.backup').exists())
        self.assertEqual(self.run_service('updates-check').returncode,10)
        self.assertEqual(self.run_service('updates-remove').returncode,0)

    def test_bootstrap_failure_has_recoverable_original_files(self):
        self.prepare()
        (self.root/'hup-fails').touch()
        self.assertNotEqual(self.run_service('updates-install').returncode,0)
        self.assertTrue((self.root/'updates.backup/runtime.json').exists())
        (self.root/'hup-fails').unlink()
        result=self.run_service('updates-remove')
        self.assertEqual(result.returncode,0,result.stderr.decode())
        self.assertEqual(self.original,{n:(self.root/n).read_bytes() for n in self.original})

    def test_actual_rule_readback_is_required(self):
        cfg=self.installed()
        cfg['rules'].insert(0,'DOMAIN,new.invalid,DIRECT')
        self.next_config(cfg)
        (self.root/'stale-rules').touch()
        self.assertNotEqual(self.run_service('update').returncode,0)
        self.assertTrue((self.root/'updates.pending').exists())
        self.assertEqual(self.status()['result'],'reload-unknown')

    def test_obsolete_group_readback_is_rejected(self):
        cfg=self.installed()
        cfg['rules'].insert(0,'DOMAIN,new.invalid,DIRECT')
        self.next_config(cfg)
        (self.root/'stale-groups').touch()
        self.assertNotEqual(self.run_service('update').returncode,0)
        self.assertTrue((self.root/'updates.pending').exists())

    def test_unknown_absence_and_process_reuse_are_not_success(self):
        self.prepare()
        self.assertEqual(self.run_service('updates-check').returncode,20)
        result=self.run_service('updates-install')
        self.assertEqual(result.returncode,0,result.stderr.decode())
        (self.root/'proc/91/stat').write_text(' '.join(['0']*21+['999']))
        self.assertNotEqual(self.run_service('updates-remove').returncode,0)
        self.assertTrue((self.root/'updates.backup').exists())
        self.assertEqual(self.run_service('updates-check').returncode,20)


    def test_failed_removal_restores_original_disk_files_and_keeps_recovery(self):
        self.prepare()
        (self.root/'hup-fails').touch()
        self.assertNotEqual(self.run_service('updates-install').returncode,0)
        self.assertNotEqual(self.run_service('updates-remove').returncode,0)
        self.assertEqual(self.original,{n:(self.root/n).read_bytes() for n in self.original})
        self.assertTrue((self.root/'updates.backup').exists())
        self.assertTrue((self.root/'updates.pending').exists())

    def test_crond_ownership_checks_executable_birth_and_entire_command(self):
        self.prepare()
        # Exercise the real ownership function independently of scheduler spawning.
        helper=self.helper.replace('/data/adb/magisk/busybox',str(self.root/'busybox'))
        proc=self.root/'proc/93'
        proc.mkdir()
        (proc/'exe').symlink_to(self.root/'busybox')
        (proc/'stat').write_text(' '.join(['0']*21+['123']))
        command=(str(self.root/'busybox')+'\0crond\0-f\0-c\0'+str(self.root/'cron')+'\0').encode()
        (proc/'cmdline').write_bytes(command)
        (self.root/'updates-crond.pid').write_text('93 123\n')
        def check():
            return subprocess.run(['sh','-c',f'cd {host.shlex.quote(str(self.root))}\nROOT={host.shlex.quote(str(self.root))}\n'+helper+'\ncron_owned'],capture_output=True).returncode
        self.assertEqual(check(),0)
        (proc/'cmdline').write_bytes(command.replace(b'\0-f\0',b'\0-b\0'))
        self.assertEqual(check(),2)
        (proc/'cmdline').write_bytes(command)
        (proc/'stat').write_text(' '.join(['0']*21+['999']))
        self.assertEqual(check(),2)
        (proc/'stat').unlink()
        self.assertEqual(check(),1)

    def test_stale_crond_record_can_reschedule_and_remove_without_signaling(self):
        self.prepare()
        helper = self.helper.replace('/data/adb/magisk/busybox', str(self.root / 'busybox'))
        helper = helper.replace('cron_owned() {', 'record_owned() {')
        self.tool('busybox', f'echo "$*" >> "{self.root}/spawned"\n')
        proc = self.root / 'proc/93'
        proc.mkdir()
        (proc / 'stat').write_text(' '.join(['0'] * 21 + ['123']))
        (proc / 'exe').symlink_to(self.root / 'busybox')
        command = (str(self.root / 'busybox') + '\0crond\0-f\0-c\0' + str(self.root / 'cron') + '\0').encode()
        (proc / 'cmdline').write_bytes(command)
        (self.root / 'updates.enabled').touch()
        # Only the newly spawned daemon's platform identity is simulated.
        # Stale records run through the real executable/starttime/cmdline checks.
        harness = f'cd {host.shlex.quote(str(self.root))}\nROOT={host.shlex.quote(str(self.root))}\n' + helper + '''
cron_owned() {
    [ -f updates-crond.pid ] || return 1
    read cron_pid cron_birth < updates-crond.pid
    if [ "$cron_pid" = 93 ]; then record_owned; else return 0; fi
}
awk() { if [ -f "$2" ]; then command awk "$@"; else echo 456; fi; }
kill() { echo "$*" >> signals; return 1; }
'''
        for fault in ('executable', 'starttime', 'command'):
            with self.subTest(fault=fault):
                (proc / 'exe').unlink()
                (proc / 'exe').symlink_to(self.root / ('unrelated' if fault == 'executable' else 'busybox'))
                (proc / 'stat').write_text(' '.join(['0'] * 21 + ['999' if fault == 'starttime' else '123']))
                (proc / 'cmdline').write_bytes(command.replace(b'\0-f\0', b'\0-b\0') if fault == 'command' else command)
                (self.root / 'updates-crond.pid').write_text('93 123\n')
                result = subprocess.run(['sh', '-ec', harness + '\nunschedule'], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertFalse((self.root / 'updates-crond.pid').exists())
                (self.root / 'updates-crond.pid').write_text('93 123\n')
                result = subprocess.run(['sh', '-ec', harness + '\nschedule'], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertNotEqual((self.root / 'updates-crond.pid').read_text().split()[0], '93')
                self.assertFalse((self.root / 'signals').exists())
        self.assertEqual((self.root / 'spawned').read_text().splitlines(), [f'crond -f -c {self.root}/cron'] * 3)

    def test_host_install_check_remove_repeat_and_failed_bootstrap(self):
        self.prepare()
        import shutil
        shutil.rmtree(self.root/'updates.stage')
        source=self.root/'source'
        source.mkdir()
        for n in ('service.sh','boot.sh','updates.sh','network.sh'):
            (source/n).write_bytes((MODULE.parent/n).read_bytes())
        (self.root/'network.sh').write_bytes((source/'network.sh').read_bytes())
        # The original deployment may already ship the update-aware service script.
        (self.root/'service.sh').write_bytes((source/'service.sh').read_bytes())
        self.tool('tailscale', '''echo '{"BackendState":"Running"}'\n''')
        self.tool('tailscaled','exit 0\n')
        pins={n:host.digest(self.root/n) for n in ('mihomo','tailscale','tailscaled')}
        (source/'artifacts.json').write_text(json.dumps({'sha256':pins}))
        self.boot.write_bytes((self.root/'boot.sh').read_bytes())
        baseline={'source_oid':'b'*40,'sha256':pins,'config_sha256':host.digest(self.root/'config.json'),
                  'service_sha256':host.digest(self.root/'service.sh'),
                  'scripts_sha256':{n:host.digest(self.root/n) for n in ('network.sh','boot.sh')},
                  'transparent':False,'autostart':True}
        (self.root/'deployment.json').write_text(json.dumps(baseline))
        (self.root/'boot-enabled').touch()
        self.script=self.script.replace('schedule() { echo schedule >>', 'schedule() { mkdir -p cron; printf "%s\\n" "$CRON_LINE" > cron/root; echo schedule >>')
        # Only daemon execution is stubbed; config/manifest/check and recovery execute in shell.
        self.script=self.script.replace('private_root() { return 0; }','private_root() { return 0; }\ncron_owned() { return 0; }')
        fixture=self
        class Device(LocalShellDevice):
            def shell(self,script,*,data=None):
                script=script.replace('/data/adb/magisk/busybox ash','sh')
                script=script.replace('/data/adb/magisk/busybox tar','tar')
                script=script.replace('/data/adb/magisk/busybox flock -n 9','true')
                script=script.replace(f'[ "$(stat -c %u:%a {fixture.root})" = 0:700 ]','true')
                return super().shell(script,data=data)
            def service(self,action):
                return fixture.run_service(action)
        url=self.root/'subscription'
        url.write_text('https://private.invalid/secret-token\n')
        url.chmod(0o600)
        args=SimpleNamespace(serial='fake',source_oid='a'*40,state_dir=str(self.root/'trial'),subscription_file=str(url))
        device=Device('fake')
        with patch.object(host,'ROOT',str(self.root)),patch.object(host,'BOOT',str(self.boot)),patch.object(host,'HERE',source):
            self.next_config({**self.config, 'external-controller-unix': str(self.root) + '/controller.sock'})
            before = {n: ((self.root / n).read_bytes(), (self.root / n).stat().st_ino,
                          (self.root / n).stat().st_mtime_ns) for n in host.UPDATE_FILES}
            (self.root / 'download-fails').touch()
            with self.assertRaisesRegex(host.OperationError, 'original running files and process identity restored'):
                host.updates_install(args, device)
            (self.root / 'download-fails').unlink()
            self.assertEqual(before, {n: ((self.root / n).read_bytes(), (self.root / n).stat().st_ino,
                                         (self.root / n).stat().st_mtime_ns) for n in host.UPDATE_FILES})
            self.assertEqual(host.updates_check(args, device), 10)
            self.assertEqual(json.loads((Path(args.state_dir) / 'updates.json').read_text())['phase'], 'restored')
            self.assertFalse((self.root / 'signals').exists())
            self.assertNotIn('schedule', (self.root / 'schedules').read_text().splitlines())
            self.assertFalse((self.root / 'updates.stage').exists())
            self.assertEqual(host.updates_install(args,device),0)
            self.assertEqual(host.check(args,device),0)
            self.assertEqual(host.updates_check(args,device),0)
            apply_state = self.root / 'apply-state'
            apply_state.mkdir()
            (apply_state / 'deployment.json').write_text(json.dumps(baseline))
            with self.assertRaisesRegex(host.OperationError, 'install state directory'):
                host.check(SimpleNamespace(**{**vars(args), 'state_dir': str(apply_state)}), device)
            record=json.loads((Path(args.state_dir)/'updates.json').read_text())
            self.assertNotIn('secret-token',json.dumps(record))
            staged_path = self.root / 'updates.stage/deployment.json'
            staged_bytes = staged_path.read_bytes()
            staged_path.write_text(json.dumps({**record['deployment'], 'source_oid': 'c' * 40}))
            with self.assertRaisesRegex(host.OperationError, 'another staged update'):
                host.updates_remove(args, device)
            self.assertTrue((self.root / 'updates.backup').exists())
            staged_path.write_bytes(staged_bytes)
            # A legitimate restart on a different uplink changes the derived runtime.
            (self.root / 'routes').write_text('')
            (self.root / 'addresses').write_text('')
            self.link('sipa_eth9')
            for pid, name in ((91, 'mihomo'), (92, 'tailscaled')):
                (self.root / f'proc/{pid}/stat').write_text(' '.join(['0'] * 21 + ['999']))
                (self.root / f'{name}.pid').write_text(f'{pid} 999\n')
            self.original['runtime.json'] = self.original['runtime.json'].replace(b'sipa_eth0', b'sipa_eth9')
            with patch.object(host, 'verify_update_baseline', side_effect=host.OperationError('verification interrupted')):
                with self.assertRaisesRegex(host.OperationError, 'verification interrupted'):
                    host.updates_remove(args, device)
            self.assertTrue((self.root / 'updates.backup').exists())
            self.assertTrue((self.root / 'updates.pending').exists())
            self.assertEqual((self.root / 'runtime.json').read_bytes(), self.original['runtime.json'])
            self.assertEqual(host.updates_remove(args,device),0)
            self.assertEqual(host.updates_check(args,device),10)
            self.assertEqual(host.updates_remove(args,device),0)
            # Upload failures never trigger removal of an existing/unknown transaction.
            with patch.object(host, 'stage_updates', side_effect=host.OperationError('upload interrupted')), \
                    patch.object(host, 'updates_remove') as remove:
                with self.assertRaisesRegex(host.OperationError, 'upload interrupted'):
                    host.updates_install(args, device)
                remove.assert_not_called()
            self.assertEqual(host.updates_remove(args, device), 0)
            stage = self.root / 'updates.stage'
            stage.mkdir()
            sentinel = stage / 'deployment.json'
            sentinel.write_bytes(b'unknown recovery material')
            with self.assertRaises(host.OperationError):
                host.stage_updates(device, {'deployment.json': b'replacement'})
            self.assertEqual(sentinel.read_bytes(), b'unknown recovery material')
            sentinel.unlink()
            stage.rmdir()
            self.assertEqual(host.updates_install(args,device),0)
            self.assertEqual(host.updates_remove(args,device),0)
            (self.root/'hup-fails').touch()
            with self.assertRaisesRegex(host.OperationError,'recovery unverified'):
                host.updates_install(args,device)
            # Disk baseline is restored even when HUP never creates the controller.
            self.assertEqual(self.original,{n:(self.root/n).read_bytes() for n in self.original})
            self.assertEqual(json.loads((Path(args.state_dir)/'updates.json').read_text())['phase'],'prepared')
            with self.assertRaisesRegex(host.OperationError,'state unknown'):
                host.updates_install(args,device)
            (self.root/'hup-fails').unlink()
            self.assertEqual(host.updates_remove(args,device),0)

    def test_start_reconfirms_runtime_digest_without_downloading_or_reloading(self):
        self.installed()
        runtime = self.root / "runtime.json"
        cfg = json.loads(runtime.read_text())
        cfg["interface-name"] = "sipa_eth9"
        runtime.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        (self.root / "loaded.json").write_bytes(runtime.read_bytes())
        self.script = self.script.replace('case "$action" in', 'case "$action" in\nresume-test) resume_updates;;')
        result = self.run_service("resume-test")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.status()["runtime_sha256"], host.digest(runtime))
        self.assertEqual(self.status()["reload_count"], "0")
        self.assertEqual((self.root / "curl-calls").read_text().count("--config"), 1)
        self.assertNotIn("-X PUT", (self.root / "curl-calls").read_text())

    def test_remove_uses_current_owned_identity_after_a_legitimate_start(self):
        self.installed()
        (self.root / 'routes').write_text('')
        (self.root / 'addresses').write_text('')
        self.link('sipa_eth9')
        for pid, name in ((91, "mihomo"), (92, "tailscaled")):
            (self.root / f"proc/{pid}/stat").write_text(" ".join(["0"] * 21 + ["999"]))
            (self.root / f"{name}.pid").write_text(f"{pid} 999\n")
        result = self.run_service("updates-remove")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        expected = {**self.original, 'runtime.json': self.original['runtime.json'].replace(b'sipa_eth0', b'sipa_eth9')}
        self.assertEqual(expected, {n: (self.root / n).read_bytes() for n in expected})
        self.assertEqual((self.root / 'loaded.json').read_bytes(), expected['runtime.json'])
        self.assertTrue((self.root / 'updates.backup').exists())
        (self.root / 'runtime.json').write_bytes(self.original['runtime.json'])
        self.assertNotEqual(self.run_service('updates-finalize').returncode, 0)
        self.assertTrue((self.root / 'updates.backup').exists())
        (self.root / 'runtime.json').write_bytes(expected['runtime.json'])
        self.assertEqual(self.run_service('updates-finalize').returncode, 0)

    def test_boot_keeps_proxy_services_when_update_recovery_fails(self):
        import shutil
        self.installed()
        initial_status = (self.root / 'update.status').read_bytes()
        script = self.script
        self.tool('tailscaled', 'exit 0\n')
        self.tool('getprop', 'echo 1\n')
        self.tool('boot-ip', "echo ' inet 192.168.0.1/24'\n")
        (self.root / 'boot-enabled').touch()
        boot = (MODULE.parent / 'boot.sh').read_text().replace(
            'ROOT=/data/adb/f50-soft-router', f'ROOT={self.root}')
        boot = boot.replace('/system/bin/getprop', str(self.root / 'getprop'))
        boot = boot.replace('/system/bin/ip', str(self.root / 'boot-ip'))
        boot = boot.replace('/data/adb/magisk/busybox ash', 'sh').replace('$ROOT/service.sh', '$ROOT/runner')
        record = '''
record() {
    case "$1" in mihomo) pid=91;; tailscaled) pid=92;; esac
    mkdir -p "$ROOT/proc/$pid"
    ln -s "$ROOT/$1" "$ROOT/proc/$pid/exe"
    awk 'BEGIN {for(i=1;i<=21;i++) printf "0 "; print 999}' > "$ROOT/proc/$pid/stat"
    echo "$pid 999" > "$1.pid"
    if [ "$1" = mihomo ]; then cp runtime.json loaded.json; fi
}
'''
        for fault in ('pending', 'readback-timeout', 'status-mismatch', 'schedule-failed'):
            with self.subTest(fault=fault):
                for name in ('updates.pending', 'stale-rules', 'active', 'mihomo.pid', 'tailscaled.pid', 'actions'):
                    (self.root / name).unlink(missing_ok=True)
                shutil.rmtree(self.root / 'proc')
                (self.root / 'update.status').write_bytes(initial_status)
                if fault == 'pending':
                    (self.root / 'updates.pending').touch()
                elif fault == 'readback-timeout':
                    (self.root / 'stale-rules').touch()
                elif fault == 'status-mismatch':
                    (self.root / 'update.status').write_bytes(initial_status.replace(b'config_sha256=', b'wrong_sha256='))
                extra = record + ('\nschedule() { return 1; }\n' if fault == 'schedule-failed' else '')
                runner = script.replace('action=${1:-}', extra + '\naction=${1:-}')
                self.tool('runner', f'echo "$1" >> "{self.root}/actions"\n' + runner)
                result = subprocess.run(['sh', '-c', boot], env=self.env, capture_output=True)
                self.assertEqual(result.returncode, 28, result.stderr.decode())
                self.assertEqual((self.root / 'actions').read_text().splitlines(), ['uplink', 'boot'])
                self.assertTrue((self.root / 'active').exists())
                self.assertEqual((self.root / 'mihomo.pid').read_text(), '91 999\n')
                self.assertEqual((self.root / 'tailscaled.pid').read_text(), '92 999\n')
                self.assertNotEqual(self.run_service('updates-check').returncode, 0)


class UpdateArgumentsTest(unittest.TestCase):
    def test_staging_keeps_private_bytes_in_stdin_and_uses_busybox_for_the_lock(self):
        with patch.object(host.Device, "require") as require:
            host.stage_updates(host.Device("fake"), {"subscription.curl": b"private-token"})
            self.assertTrue(require.call_args.args[0].startswith("/data/adb/magisk/busybox ash -c "))
            self.assertNotIn("private-token", require.call_args.args[0])
            self.assertIn(b"private-token", require.call_args.kwargs["data"])

    def test_required_identity_and_missing_shared_adapter(self):
        for oid in (None,'a'*7,'g'*40):
            with self.assertRaises(host.OperationError):
                host.update_args(SimpleNamespace(source_oid=oid,state_dir='/tmp/unused'))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json'
            path.write_text('{"rules":["MATCH,DIRECT"],"proxies":[{}]}')
            with patch.dict(os.environ,{'F50_PROXY_CONFIG_ROOT':directory}):
                with self.assertRaisesRegex(host.OperationError,'Missing central F50 adapter'):
                    host.configuration(path)

    def test_private_url_boundary_precedes_device_mutation(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'url'
            args=SimpleNamespace(source_oid='a'*40,state_dir=directory,subscription_file=str(path))
            device=Mock()
            for value in ('http://invalid/token','https://invalid/\nurl = bad','https://user:password@invalid/token',
                          'https://invalid/"token','https://invalid/\\token'):
                path.write_text(value);path.chmod(0o600)
                with self.assertRaises(host.OperationError): host.updates_install(args,device)
            path.write_text('https://invalid/token');path.chmod(0o644)
            with self.assertRaises(host.OperationError): host.updates_install(args,device)
            device.assert_not_called()
            self.assertEqual(device.mock_calls,[])


if __name__ == "__main__":
    unittest.main()
