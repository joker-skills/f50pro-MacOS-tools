import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_soft_router import MODULE, host


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ("recovery.enabled", "active"):
            (self.root / name).touch()

    def run_shell(self, script):
        helper = MODULE.with_name("recovery.sh")
        result = subprocess.run(["sh", "-eu", "-c", '. "$2"\n' + script, "--",
                                 str(self.root), str(helper)], env={**os.environ, "ROOT": str(self.root)},
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result.stdout

    def decisions(self, script):
        return self.run_shell("""
boot=fixture-one now=1000 mode=1
recovery_boot() { echo "$boot"; }
recovery_now() { echo "$now"; }
recovery_allowed() { [ "$mode" != off ]; }
recovery_probe() { [ "$mode" = healthy ]; }
recovery_capture() { echo "capture $now" >> "$ROOT/actions"; }
recovery_redial() { echo "redial $now" >> "$ROOT/actions"; return 1; }
tick() { recovery_tick; now=$((now+60)); }
three() { tick; tick; tick; }
count() { grep -c '^redial' "$ROOT/actions" || :; }
touch "$ROOT/actions"
""" + script)

    def test_three_rounds_capture_before_attempt_and_both_resets(self):
        self.decisions("""
now=120
three
[ "$(count)" = 0 ]
now=1000
tick; tick
mode=off; tick; mode=1
tick; tick
mode=healthy; tick; mode=1
tick; tick
[ "$(count)" = 0 ]
tick
[ "$(count)" = 1 ]
""")
        self.assertEqual((self.root / "actions").read_text().splitlines(),
                         ["capture 1480", "redial 1480"])

    def test_failed_attempts_spend_budget_and_boot_and_bad_state_hold_it(self):
        self.decisions("""
three
now=1600; tick; tick; now=1719; tick
[ "$(count)" = 1 ]
now=1720; tick
now=2200; tick; tick; tick
[ "$(count)" = 3 ]
now=4300; three
[ "$(count)" = 3 ]
now=4600; tick; tick; now=4720; tick
[ "$(count)" = 4 ]
boot=fixture-two now=30; tick
now=700; three
[ "$(count)" = 4 ]
echo broken > "$ROOT/recovery/state"
now=5000; three
[ "$(count)" = 4 ]
now=8600; three
[ "$(count)" = 5 ]
""")
        self.assertIn("budget=held", (self.root / "recovery/events.log").read_text())

    def test_probe_uses_bound_head_counts_http_error_and_rejects_local_failure(self):
        self.run_shell("""
rec_if=sipa_eth0
curl() {
    case "$*" in
      '-q -4 --silent --noproxy * --interface if!sipa_eth0 '*)
        case "$*" in *'--head --output /dev/null --max-time 8 '*) ;; *) exit 90;; esac ;;
      *) exit 90;;
    esac
    case "$*" in *--location*|*--fail*) exit 90;; esac
    echo "${17:-probe}" >> "$ROOT/probes"
    case "$*" in
        *www.baidu.com*) echo '1.2.3.4 000 0.020 0.000 8.000'; return 28;;
        *www.qq.com*) echo '2.3.4.5 501 0.020 0.030 0.040'; return 0;;
        *) exit 90;;
    esac
}
recovery_probe
[ "$(wc -l < "$ROOT/probes")" -eq 2 ]
case "$rec_probes" in *'www.qq.com rc=0 ip/http/connect/tls/total=2.3.4.5 501 '*) ;; *) exit 91;; esac
curl() { return 45; }
rc=0; recovery_probe || rc=$?
[ "$rc" = 2 ]
""")

    def test_interrupted_disable_is_rescued_but_manual_off_is_not(self):
        self.run_shell("""
rec_dir=$ROOT/recovery rec_boot=fixture rec_now=1000
mkdir "$rec_dir"
recovery_flush() { [ ! -e "$ROOT/fail-flush" ]; }
printf '1\n' > "$ROOT/data"
printf '0\n' > "$ROOT/air"
recovery_setting() {
    case "$1" in mobile_data) cat "$ROOT/data";; airplane_mode_on) cat "$ROOT/air";; esac
}
recovery_boot() { echo "$rec_boot"; }
recovery_now() { echo "$rec_now"; }
recovery_phone() {
    echo "$1" >> "$ROOT/toggles"
    if [ "$1" = disable ]; then echo 0 > "$ROOT/data"; return 1; fi
    [ ! -e "$ROOT/fail-enable" ] || return 1
    echo 1 > "$ROOT/data"
}
touch "$ROOT/fail-enable"
touch "$ROOT/fail-flush"
if recovery_redial; then exit 91; fi
[ ! -e "$rec_dir/pending" ] && [ ! -e "$ROOT/toggles" ]
rm "$ROOT/fail-flush"
if recovery_redial; then exit 91; fi
[ -e "$rec_dir/pending" ]
rm "$ROOT/fail-enable"
printf '1\n' > "$ROOT/air"
recovery_tick
[ -e "$rec_dir/pending" ]
printf '0\n' > "$ROOT/air"
recovery_tick
[ ! -e "$rec_dir/pending" ]
[ "$(cat "$ROOT/data")" = 1 ]
printf '0\n' > "$ROOT/data"
recovery_tick
[ "$(cat "$ROOT/data")" = 0 ]
printf '1\n' > "$ROOT/data"
recovery_phone() { echo "$1" >> "$ROOT/toggles"; }
sleep() { :; }
recovery_redial
[ ! -e "$rec_dir/pending" ]
""")
        self.assertEqual((self.root / "toggles").read_text().splitlines(),
                         ["disable", "enable", "enable", "disable", "enable"])

    def test_diagnostics_are_bounded_private_and_low_space_does_not_gate_recovery(self):
        self.run_shell("""
umask 077
rec_dir=$ROOT/recovery rec_now=1200 rec_boot=fixture rec_if=sipa_eth0 rec_data=1 rec_air=0 rec_probes=fixture
mkdir "$rec_dir"
df() { printf 'Filesystem blocks used available capacity mount\n/tmp 8000 1 7999 1%% /tmp\n'; }
i=0
while [ "$i" -lt 12 ]; do
    recovery_capture
    i=$((i+1))
done
dd if=/dev/zero of="$rec_dir/events.log" bs=1024 count=64 2>/dev/null
recovery_log 'rotation'
df() { printf 'Filesystem blocks used available capacity mount\n/tmp 8000 7900 100 99%% /tmp\n'; }
recovery_boot() { echo fixture; }
recovery_now() { echo "$rec_now"; }
recovery_allowed() { return 0; }
recovery_probe() { return 1; }
recovery_redial() { touch "$ROOT/attempted"; return 1; }
recovery_tick
rec_now=1260; recovery_tick
rec_now=1320; recovery_tick
[ -e "$ROOT/attempted" ]
""")
        directory = self.root / "recovery"
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        incidents = list(directory.glob("incident.*"))
        self.assertEqual(len(incidents), 10)
        self.assertTrue(all(path.stat().st_size <= 8192 for path in incidents))
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir()))
        self.assertIn("capture=unavailable", (directory / "events.log").read_text())
        self.assertEqual((directory / "events.previous").stat().st_size, 65536)


class RecoveryDeploymentTest(unittest.TestCase):
    def test_overlay_transaction_interruption_and_restore_preserve_running_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            boot = root / "boot-entry"
            class Device(host.Device):
                def shell(self, script, *, data=None):
                    # Only platform tooling/process reads are replaced; writes and guards run in shell.
                    script = script.replace("/data/adb/magisk/busybox ash", "sh")
                    script = script.replace("/data/adb/magisk/busybox tar", "tar")
                    script = script.replace(
                        "/data/adb/magisk/busybox timeout -k 2 5 /data/adb/magisk/busybox sync", "true")
                    script = script.replace("/proc/sys/kernel/random/boot_id", "boot-id")
                    script = script.replace("sha256sum ", "shasum -a 256 ")
                    script = re.sub(r'stat -c %u:%a ([^ )]+)', r"stat -f '0:%Lp' \1", script)
                    script = script.replace("stat -c %Y cron", "stat -f %m cron")
                    script = script.replace("/data/adb/magisk/busybox flock -n 9", "fixture_lock")
                    preamble = ("fixture_lock() { [ ! -e " + host.shlex.quote(str(root / "busy")) +
                                " ]; }; export -f fixture_lock; ")
                    return subprocess.run(["bash", "-c", preamble + script], input=data, capture_output=True)
            device = Device("fixture")
            args = SimpleNamespace(serial=device.serial, source_oid="a"*40, state_dir=str(root / "backup"))
            before = {"service.sh": b"old service", "updates.sh": b"old updates", "boot.sh": b"old boot"}
            sources = {n: MODULE.with_name(n).read_bytes() for n in host.RECOVERY_SCRIPTS}
            with patch.object(host, "ROOT", str(root)), patch.object(host, "BOOT", str(boot)), \
                    patch.object(host, "recovery_source", side_effect=lambda _oid: sources.copy()), \
                    patch.object(device, "service", return_value=SimpleNamespace(returncode=0)):
                base_cron, recovery_line = host.recovery_cron()
                base_hashes = host.recovery_sha(before)
                baseline = {"autostart": True, "source_oid": "b"*40,
                            "service_sha256": base_hashes["service.sh"], "scripts_sha256": base_hashes}
                stable = {"config.json": b"opaque-config", "runtime.json": b"opaque-runtime",
                          "subscription.curl": b"opaque-subscription", "deployment.json": json.dumps(baseline).encode(),
                          "mihomo.pid": b"101 1001", "tailscaled.pid": b"102 1002",
                          "updates-crond.pid": b"103 1003", "boot-id": b"boot-1",
                          "auth/enabled": b"auth-marker"}
                (root / "cron").mkdir()
                (root / "auth").mkdir()
                for name, content in {**before, **stable, "boot-entry": before["boot.sh"],
                                      "cron/root": base_cron, "updates.enabled": b"",
                                      "owner": b"f50-soft-router-v1"}.items():
                    path = root / name
                    path.write_bytes(content)
                    path.chmod(0o700 if name in (*before, "boot-entry") else 0o600)
                def healthy(_args, target):
                    host.recovery_effective(target, baseline)
                    return 0
                with patch.object(host, "check", side_effect=healthy):
                    with patch.object(host, "recovery_transaction", side_effect=host.OperationError("fixture interruption")):
                        with self.assertRaisesRegex(host.OperationError, "interruption"):
                            host.recovery_install(args, device)
                    # Simulate termination after the helper and one script moved, before enablement.
                    for name in ("service.sh", "recovery.sh"):
                        (root / name).write_bytes(sources[name])
                        (root / name).chmod(0o700)
                    self.assertEqual(host.recovery_remove(args, device), 0)
                    (Path(args.state_dir) / "recovery.json").unlink()
                    self.assertEqual(host.recovery_install(args, device), 0)
                    installed_cron = (root / "cron/root").read_bytes()
                    self.assertEqual(installed_cron, base_cron + recovery_line)
                    self.assertEqual(subprocess.check_output(
                        ["sh", "-ec", '. ./updates.sh; cron_contents'], cwd=root,
                        env={**os.environ, "ROOT": str(root)}), installed_cron)
                    self.assertEqual(host.recovery_check(args, device), 0)
                    with self.assertRaisesRegex(host.OperationError, "overlay first"):
                        host.reject_recovery(device)
                    (root / "busy").touch()
                    with self.assertRaisesRegex(host.OperationError, "exit 28"):
                        host.recovery_remove(args, device)
                    (root / "busy").unlink()
                    (root / "recovery").mkdir()
                    pending = root / "recovery/pending"
                    pending.touch()
                    with self.assertRaises(host.OperationError):
                        host.recovery_remove(args, device)
                    pending.unlink()
                    (root / "cron/root").write_bytes(b"later unrelated change")
                    with self.assertRaises(host.OperationError):
                        host.recovery_remove(args, device)
                    self.assertTrue((root / "recovery.enabled").exists())
                    (root / "cron/root").write_bytes(base_cron + recovery_line)
                    (root / "updates.pending").write_bytes(b"unresolved update")
                    self.assertEqual(host.recovery_remove(args, device), 0)
                    self.assertEqual(host.recovery_restore_check(args, device), 0)
                    self.assertEqual((root / "cron/root").read_bytes(), base_cron)
                    self.assertEqual((root / "updates.pending").read_bytes(), b"unresolved update")
                    self.assertEqual({n: (root / n).read_bytes() for n in stable}, stable)
