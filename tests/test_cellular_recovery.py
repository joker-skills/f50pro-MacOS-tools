import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
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
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result.stdout

    clock = """
at() { printf '%s\n' "$1" > "$ROOT/clock"; }
recovery_now() { cat "$ROOT/clock"; }
sleep() { at "$(($(recovery_now) + $1))"; }
at 1000
"""

    def decisions(self, script):
        return self.run_shell(self.clock + """
boot=fixture-one
recovery_boot() { echo "$boot"; }
recovery_allowed() { return 0; }
recovery_probe() { return 1; }
recovery_capture() { echo "capture $(recovery_now)" >> "$ROOT/actions"; }
recovery_redial() { echo "redial $(recovery_now)" >> "$ROOT/actions"; }
count() { grep -c '^redial' "$ROOT/actions" || :; }
touch "$ROOT/actions"
""" + script)

    def test_boot_grace_confirmation_and_first_success(self):
        self.decisions("""
recovery_probe() {
    echo "probe $(recovery_now)" >> "$ROOT/actions"
    [ "$(count)" = 1 ]
}
at 59; recovery_tick
[ "$(count)" = 0 ]
at 60; recovery_tick
[ "$(count)" = 1 ]
""")
        self.assertEqual((self.root / "actions").read_text().splitlines(),
                         ["probe 60", "probe 75", "capture 75", "redial 75", "probe 75"])

    def test_first_failure_retries_once_and_two_failures_cool_down(self):
        self.decisions("""
recovery_tick
[ "$(count)" = 2 ]
at 1135; recovery_tick
at 1673; recovery_tick
[ "$(count)" = 2 ]
at 1674; recovery_tick
[ "$(count)" = 3 ]
at 2300; recovery_tick
at 4614; recovery_tick
[ "$(count)" = 3 ]
""")
        self.assertEqual((self.root / "actions").read_text().splitlines(),
                         ["capture 1015", "redial 1015", "redial 1074",
                          "capture 1689", "redial 1689"])
        self.assertEqual((self.root / "recovery/events.log").read_text().count("attempt=post-failed"), 3)

    def test_reboot_and_bad_state_hold_budget(self):
        self.decisions("""
recovery_tick
boot=fixture-two
at 60; recovery_tick
at 3599; recovery_tick
[ "$(count)" = 2 ]
at 3600; recovery_tick
[ "$(count)" = 4 ]
echo broken > "$ROOT/recovery/state"
at 5000; recovery_tick
at 8599; recovery_tick
[ "$(count)" = 4 ]
at 8600; recovery_tick
[ "$(count)" = 6 ]
""")
        self.assertIn("budget=held", (self.root / "recovery/events.log").read_text())

    def test_boot_without_recent_attempt_uses_only_normal_grace(self):
        self.decisions("""
recovery_probe() { [ "$boot" = fixture-one ]; }
recovery_tick
boot=fixture-two
at 60; recovery_tick
[ "$(count)" = 2 ]
""")

    def test_reconfirmation_and_post_probe_can_stop_recovery(self):
        self.decisions("""
recovery_probe() {
    case "$(recovery_now)" in
        1000|2000|3000|3015) return 1;;
        1015) return 0;;
        *) return 2;;
    esac
}
recovery_tick
at 2000; recovery_tick
[ "$(count)" = 0 ]
recovery_probe() {
    [ "$(count)" = 0 ] && return 1
    return 2
}
at 3000; recovery_tick
[ "$(count)" = 1 ]
""")
        log = (self.root / "recovery/events.log").read_text()
        for event in ("healthy", "probe-unavailable", "attempt=post-unavailable"):
            self.assertIn(f" {event} ", log)

    def test_post_deadline_includes_checks_curl_and_sleep(self):
        self.run_shell(self.clock + """
BB=busybox_fixture
busybox_fixture() {
    [ "$1:$2:$3" = timeout:-k:1 ] || exit 90
    limit=$4
    shift 4
    case "$*" in
        '/system/bin/settings get global mobile_data') elapsed=3; value=1;;
        '/system/bin/settings get global airplane_mode_on') elapsed=2; value=0;;
        *'service.sh uplink') elapsed=2; value=sipa_eth0;;
        *) exit 91;;
    esac
    [ "$((limit + 1 + $(recovery_now)))" -lt 1060 ] || exit 92
    [ "$elapsed" -le "$limit" ] || return 1
    sleep "$elapsed"
    echo "$value"
}
curl() {
    while [ "$1" != --max-time ]; do shift; done
    limit=$2
    [ "$((limit + $(recovery_now)))" -lt 1060 ] || exit 93
    echo "$limit" >> "$ROOT/timeouts"
    sleep "$limit"
    return 28
}
recovery_post
[ "$rec_outcome" = post-failed ]
[ "$(recovery_now)" -le 1060 ]
""")
        self.assertEqual((self.root / "timeouts").read_text().splitlines(),
                         ["8", "8", "8", "8", "8", "2"])

    def test_no_uplink_after_redial_is_rechecked_until_available(self):
        self.decisions("""
recovery_allowed() {
    rec_reason=no-cellular-uplink
    [ "$(count)" = 0 ] || [ "$(recovery_now)" -ge 1021 ]
}
recovery_probe() { [ "$(count)" = 1 ]; }
recovery_tick
[ "$(recovery_now)" = 1021 ]
[ "$(count)" = 1 ]
""")
        self.assertIn("attempt=post-healthy", (self.root / "recovery/events.log").read_text())

    def test_post_deadline_preserves_failure_for_retry_with_fresh_settings(self):
        for scenario, outcomes in (
            ("retry", ["post-failed", "post-healthy"]),
            ("fresh-manual-off", ["post-failed"]),
            ("manual-off", ["post-unavailable"]),
            ("settings-error", ["post-unavailable"]),
        ):
            with self.subTest(scenario=scenario):
                (self.root / "scenario").write_text(scenario)
                self.run_shell(self.clock + """
scenario=$(cat "$ROOT/scenario")
ROOT=$ROOT/$scenario
mkdir "$ROOT"
touch "$ROOT/active" "$ROOT/recovery.enabled" "$ROOT/attempts"
at 1000
BB=busybox_fixture
busybox_fixture() {
    shift 4
    case "$*" in
        '/system/bin/settings get global mobile_data')
            case "$(recovery_now):${rec_deadline:-}" in
                1042:1075) sleep 3;;
                1072:1075)
                    sleep 1
                    case "$scenario" in
                        manual-off) echo 0; return 0;;
                        settings-error) return 3;;
                    esac
                    ;;
                1073:) if [ "$scenario" = fresh-manual-off ]; then echo 0; return 0; fi;;
            esac
            echo 1;;
        '/system/bin/settings get global airplane_mode_on')
            if [ "$(recovery_now):${rec_deadline:-}" = 1073:1075 ]; then
                touch "$ROOT/late-settings"
            fi
            echo 0;;
        *'service.sh uplink') uplink;;
        *) exit 90;;
    esac
}
uplink() { echo sipa_eth0; }
recovery_boot() { echo fixture; }
recovery_capture() { :; }
recovery_redial() { recovery_now >> "$ROOT/attempts"; }
recovery_probe() {
    case "$(wc -l < "$ROOT/attempts" | tr -d ' ')" in
        0) return 1;;
        1) sleep 24; return 1;;
        2) return 0;;
    esac
}
recovery_tick
[ ! -e "$ROOT/late-settings" ]
""")
                directory = self.root / scenario
                log = (directory / "recovery/events.log").read_text()
                self.assertEqual(re.findall(r" attempt=(\S+)", log), outcomes)
                expected_attempts = ["1015", "1073"] if scenario == "retry" else ["1015"]
                self.assertEqual((directory / "attempts").read_text().splitlines(), expected_attempts)

    def test_real_redial_checks_safety_after_persisting_each_attempt(self):
        for reason in ("manual-off", "airplane", "unknown", "no-uplink", "flush"):
            with self.subTest(reason=reason):
                (self.root / "scenario").write_text(reason)
                self.run_shell(self.clock + """
recovery_boot() { echo fixture; }
recovery_capture() { :; }
recovery_probe() { return 1; }
recovery_setting() {
    case "$1" in
        mobile_data) cat "$ROOT/data";;
        airplane_mode_on) cat "$ROOT/air";;
    esac
}
uplink() { [ ! -e "$ROOT/no-uplink" ] && echo sipa_eth0; }
recovery_timeout() { uplink; }
recovery_flush() {
    read -r boot last streak a1 a2 a3 hold < "$rec_dir/state"
    [ "$last" = "$rec_now" ] && [ "$a1" = "$rec_now" ] && [ -f "$rec_dir/pending" ] || exit 90
    if [ "$a2" = 0 ]; then return 0; fi
    case "$(cat "$ROOT/scenario")" in
        manual-off) echo 0 > "$ROOT/data";;
        airplane) echo 1 > "$ROOT/air";;
        unknown) echo null > "$ROOT/data";;
        no-uplink) touch "$ROOT/no-uplink";;
        flush) return 1;;
    esac
}
recovery_phone() {
    echo "$1" >> "$ROOT/toggles"
    case "$1" in
        disable) echo 0 > "$ROOT/data";;
        enable) echo 1 > "$ROOT/data";;
    esac
}
rm -rf "$ROOT/recovery"
rm -f "$ROOT/no-uplink" "$ROOT/toggles"
echo 1 > "$ROOT/data"
echo 0 > "$ROOT/air"
recovery_tick
[ ! -e "$ROOT/recovery/pending" ]
""")
                self.assertEqual((self.root / "toggles").read_text().splitlines(), ["disable", "enable"])

    def test_reconfirmation_checks_uplink_and_state_write_failure_blocks_redial(self):
        self.decisions("""
recovery_allowed() {
    rec_reason=no-cellular-uplink
    [ "$(recovery_now)" != 1015 ]
}
recovery_tick
[ "$(count)" = 0 ]
recovery_capture() { mkdir "$rec_dir/state.tmp"; }
at 2000
rc=0; recovery_tick || rc=$?
[ "$rc" = 20 ] && [ "$(count)" = 0 ]
""")
        self.assertIn("skipped=no-cellular-uplink", (self.root / "recovery/events.log").read_text())

    def test_lifecycle_lock_skips_overlapping_tick(self):
        (self.root / "owner").write_text("f50-soft-router-v1\n")
        (self.root / "recovery.sh").write_text('recovery_tick() { touch "$ROOT/entered"; }\n')
        fixture_lock = shlex.join([sys.executable, "-c",
                                   "import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)"])
        service = MODULE.with_name("service.sh").read_text().replace(
            "ROOT=/data/adb/f50-soft-router", "ROOT=" + shlex.quote(str(self.root))
        ).replace("/data/adb/magisk/busybox flock -n 9", fixture_lock + " 2>/dev/null")
        command = ["sh", "-c", service, "--", "recovery-tick"]
        with (self.root / "lifecycle.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(subprocess.run(command, timeout=5).returncode, 28)
            self.assertFalse((self.root / "entered").exists())
        self.assertEqual(subprocess.run(command, timeout=5).returncode, 0)
        self.assertTrue((self.root / "entered").exists())

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
uplink() { echo sipa_eth0; }
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

    def test_expired_or_previous_boot_pending_never_opens_manual_off(self):
        self.run_shell("""
recovery_boot() { echo current; }
recovery_now() { echo 1181; }
recovery_setting() { echo 0; }
recovery_phone() { echo "$1" >> "$ROOT/toggles"; }
mkdir "$ROOT/recovery"
for marker in 'current 1000' 'previous 1180'; do
    printf '%s\n' "$marker" > "$ROOT/recovery/pending"
    recovery_tick
    [ ! -e "$ROOT/recovery/pending" ] && [ ! -e "$ROOT/toggles" ]
done
[ "$(grep -c 'pending=expired' "$ROOT/recovery/events.log")" = 2 ]
""")

    def test_incident_cap_includes_post_redial_result(self):
        self.run_shell(self.clock + """
recovery_boot() { echo fixture; }
recovery_allowed() { rec_if=sipa_eth0 rec_data=1 rec_air=0; }
recovery_probe() { rec_probes=$(printf '%600s' probe); return 1; }
recovery_redial() { return 0; }
df() { printf 'Filesystem blocks used available capacity mount\n/tmp 8000 1 7999 1%% /tmp\n'; }
cat() {
    if [ "$1" = /proc/net/sockstat ]; then
        yes counters | head -c 12000
    else command cat "$@"; fi
}
recovery_tick
""")
        incident = (self.root / "recovery/incident.0").read_bytes()
        self.assertLessEqual(len(incident), 8192)
        self.assertEqual(incident[7680:].count(b"attempt=post-failed"), 2)

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
sleep() { :; }
recovery_tick
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
            sources["service.sh"] = b'[ "$1" = check ]\n'
            with patch.object(host, "ROOT", str(root)), patch.object(host, "BOOT", str(boot)), \
                    patch.object(host, "recovery_source", side_effect=lambda _oid: sources.copy()) as source, \
                    patch.object(device, "service", return_value=SimpleNamespace(returncode=0)):
                base_cron, recovery_line = host.recovery_cron()
                base_hashes = host.recovery_sha(before)
                binaries = {"mihomo": b"proxy", "tailscaled": b"daemon",
                            "tailscale": b'#!/bin/sh\necho \'{"BackendState":"Running"}\'\n'}
                baseline = {"autostart": True, "source_oid": "b"*40, "transparent": False,
                            "service_sha256": base_hashes["service.sh"], "sha256": host.recovery_sha(binaries),
                            "scripts_sha256": {**base_hashes, **host.recovery_sha({"network.sh": b"network"})}}
                stable = {"config.json": b"opaque-config", "runtime.json": b"opaque-runtime",
                          "subscription.curl": b"opaque-subscription", "deployment.json": json.dumps(baseline).encode(),
                          "mihomo.pid": b"101 1001", "tailscaled.pid": b"102 1002",
                          "updates-crond.pid": b"103 1003", "boot-id": b"boot-1",
                          "auth/enabled": b"auth-marker"}
                (root / "cron").mkdir()
                (root / "auth").mkdir()
                for name, content in {**before, **stable, "boot-entry": before["boot.sh"],
                                      "cron/root": base_cron, "updates.enabled": b"",
                                      "boot-enabled": b"", "mode": b"explicit", "network.sh": b"network",
                                      **binaries, "owner": b"f50-soft-router-v1"}.items():
                    path = root / name
                    path.write_bytes(content)
                    path.chmod(0o700 if name in (*before, *binaries, "boot-entry") else 0o600)
                def healthy(_args, target):
                    host.recovery_effective(target, baseline)
                    return 0
                with patch.object(host, "check", side_effect=healthy):
                    self.assertEqual(host.recovery_check(args, device), 10)
                    marker = root / "recovery.enabled"
                    for content in (b"", b"{broken"):
                        marker.write_bytes(content)
                        marker.chmod(0o600)
                        with self.assertRaises((host.OperationError, json.JSONDecodeError)):
                            host.recovery_effective(device, baseline)
                        with self.assertRaises(host.OperationError):
                            host.recovery_check(args, device)
                    marker.unlink()
                    with patch.object(host, "recovery_transaction", side_effect=host.OperationError("fixture interruption")):
                        with self.assertRaisesRegex(host.OperationError, "interruption"):
                            host.recovery_install(args, device)
                    self.assertEqual(host.recovery_check(args, device), 10)
                    backup = Path(args.state_dir) / "recovery.json"
                    original_backup = backup.read_bytes()
                    # Simulate termination after the helper and one script moved, before enablement.
                    for name in ("service.sh", "recovery.sh"):
                        (root / name).write_bytes(sources[name])
                        (root / name).chmod(0o700)
                    with self.assertRaises(host.OperationError):
                        host.recovery_check(args, device)
                    marker.write_bytes(b"{externally altered")
                    marker.chmod(0o600)
                    with self.assertRaises(host.OperationError):
                        host.recovery_remove(args, device)
                    self.assertEqual(marker.read_bytes(), b"{externally altered")
                    marker.unlink()
                    self.assertEqual(host.recovery_remove(args, device), 0)
                    self.assertEqual(host.recovery_install(args, device), 0)
                    self.assertEqual(backup.read_bytes(), original_backup)
                    installed_cron = (root / "cron/root").read_bytes()
                    self.assertEqual(installed_cron, base_cron + recovery_line)
                    self.assertEqual(subprocess.check_output(
                        ["sh", "-ec", '. ./updates.sh; cron_contents'], cwd=root,
                        env={**os.environ, "ROOT": str(root)}), installed_cron)
                    self.assertEqual(host.recovery_check(args, device), 0)
                    with patch.object(host, "HERE", root / "unrelated-checkout"), \
                            patch.object(host, "check", side_effect=AssertionError("moving checkout used")):
                        self.assertEqual(host.recovery_check(args, device), 0)
                        source.assert_called_with(args.source_oid)
                        (root / "network.sh").write_bytes(b"drifted")
                        with self.assertRaisesRegex(host.OperationError, "differs from the baseline"):
                            host.recovery_check(args, device)
                        (root / "network.sh").write_bytes(b"network")
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
                    self.assertEqual(host.recovery_check(args, device), 10)
                    self.assertEqual((root / "cron/root").read_bytes(), base_cron)
                    self.assertEqual((root / "updates.pending").read_bytes(), b"unresolved update")
                    self.assertEqual({n: (root / n).read_bytes() for n in stable}, stable)
                    (root / "updates.pending").unlink()
                    self.assertEqual(host.recovery_install(args, device), 0)
                    self.assertEqual(backup.read_bytes(), original_backup)
