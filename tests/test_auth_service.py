"""Local filesystem/protocol fixtures only; these are not Android acceptance tests."""
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parents[1] / "device/soft-router"
spec = importlib.util.spec_from_file_location("auth_host", DIRECTORY / "host.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class AuthHostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name).resolve() / "state"
        self.args = SimpleNamespace(serial="fixture", source_oid="a" * 40,
                                    state_dir=str(self.state), injector=None, bundle=None)

    def files(self):
        files = {"frida-inject": b"ELF", "auth-hook.bundle.js": b"bundle",
                 "auth-service.sh": b"service", "boot-entry": b"boot",
                 "source_oid": (self.args.source_oid + "\n").encode()}
        files["manifest"] = "".join(hashlib.sha256(b).hexdigest() + "  " + n + "\n"
                                      for n, b in files.items()).encode()
        files["manifest.sha256"] = (hashlib.sha256(files["manifest"]).hexdigest() + "\n").encode()
        return files

    def test_private_backup_reusable_and_corruption_never_overwritten(self):
        state = host.auth_state(self.args)
        files = self.files()
        record = host.auth_backup(state, self.args, files)
        self.assertEqual(host.auth_read_backup(state, self.args), (record, files))
        self.assertEqual(host.auth_backup(state, self.args, files), record)
        (state / "boot-entry").write_bytes(b"broken")
        with self.assertRaisesRegex(host.OperationError, "corrupt"):
            host.auth_backup(state, self.args, files)
        self.assertEqual((state / "boot-entry").read_bytes(), b"broken")

    def test_rejects_public_relative_or_symlink_state_and_wrong_identity(self):
        self.args.state_dir = "relative"
        with self.assertRaises(host.OperationError):
            host.auth_state(self.args)
        self.args.state_dir = str(self.state)
        self.state.mkdir(mode=0o755)
        with self.assertRaises(host.OperationError):
            host.auth_state(self.args)
        self.state.chmod(0o700)
        host.auth_backup(self.state, self.args, self.files())
        self.args.serial = "other"
        with self.assertRaisesRegex(host.OperationError, "different identity"):
            host.auth_read_backup(self.state, self.args)
        self.args.serial = "fixture"
        (self.state / "boot-entry").unlink()
        (self.state / "boot-entry").symlink_to(self.state / "source_oid")
        with self.assertRaisesRegex(host.OperationError, "unsafe"):
            host.auth_read_backup(self.state, self.args)

    def test_rejects_unpinned_assets_before_any_device_call(self):
        path = Path(self.tmp.name) / "injector"
        path.write_bytes(b"wrong ELF")
        self.args.injector = str(path)
        with self.assertRaisesRegex(host.OperationError, "pinned"):
            host.auth_install(self.args, object())

    def test_install_backs_up_before_upload_and_remove_preserves_backup(self):
        files = self.files()
        injector = Path(self.tmp.name) / "injector"
        bundle = Path(self.tmp.name) / "bundle"
        injector.write_bytes(files["frida-inject"])
        bundle.write_bytes(files["auth-hook.bundle.js"])
        self.args.injector, self.args.bundle = str(injector), str(bundle)
        calls = []

        class Device:
            def require(inner, script):
                self.assertTrue((self.state / "auth.json").is_file())
                calls.append(script)

        def service(device, action, *, data=None):
            self.assertTrue((self.state / "auth.json").is_file())
            calls.append(action)
            if action.startswith("install "):
                import tarfile
                with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                    self.assertEqual(len(archive.getnames()), 7)
                    boot = archive.extractfile("boot-entry").read().decode()
                    self.assertNotIn("soft-router.sh", boot)
                    self.assertIn("auth-service.sh", boot)
                    self.assertIn("nohup", boot)
            return SimpleNamespace(returncode=0)

        with patch.object(host, "AUTH_INJECTOR_SHA", hashlib.sha256(files["frida-inject"]).hexdigest()), \
                patch.object(host, "AUTH_BUNDLE_SHA", hashlib.sha256(files["auth-hook.bundle.js"]).hexdigest()), \
                patch.object(host, "auth_service", side_effect=service), patch.object(host, "auth_check", return_value=0):
            self.assertEqual(host.auth_install(self.args, Device()), 0)
            self.assertEqual(host.auth_install(self.args, Device()), 0)
            self.assertEqual(host.auth_remove(self.args, Device()), 0)
        self.assertTrue((self.state / "auth.json").is_file())
        self.assertTrue(any(c.startswith("remove ") and c.endswith(" " + self.args.source_oid) for c in calls))

    def test_check_does_not_call_stale_probe_fresh(self):
        status = {"source_oid": "a" * 40, "state": "healthy", "files_healthy": True,
                  "target": {"pid": 3}, "probe": {"nonce": "old", "passed": True}}
        for code in (0, 20):
            output = io.StringIO()
            result = SimpleNamespace(returncode=code, stdout=json.dumps(status).encode())
            with patch.object(host, "auth_service", return_value=result), contextlib.redirect_stdout(output):
                exit_code = host.auth_check(self.args, object())
            report = json.loads(output.getvalue())
            self.assertEqual(report["probe"]["fresh"], code == 0)
            self.assertEqual(report["probe"]["passed"], code == 0)
            self.assertEqual(exit_code, code)

    def test_uninstalled_check_keeps_exit_10_even_with_requested_source(self):
        status = {"state": "uninstalled", "source_oid": None, "probe": {"fresh": False, "passed": False}}
        output = io.StringIO()
        with patch.object(host, "auth_service", return_value=SimpleNamespace(returncode=10, stdout=json.dumps(status).encode())), \
                contextlib.redirect_stdout(output):
            self.assertEqual(host.auth_check(self.args, object()), 10)
        self.assertEqual(json.loads(output.getvalue())["state"], "uninstalled")

    def test_restore_check_binds_valid_backup_before_read_only_device_query(self):
        state = host.auth_state(self.args)
        host.auth_backup(state, self.args, self.files())
        for code in (0, 20):
            output = io.StringIO()
            with patch.object(host, "auth_service", return_value=SimpleNamespace(returncode=code)) as service, \
                    contextlib.redirect_stdout(output):
                self.assertEqual(host.auth_restore_check(self.args, object()), code)
            self.assertEqual(service.call_args.args[1], "restore-check")
            self.assertEqual(json.loads(output.getvalue())["restored"], code == 0)
        (state / "source_oid").write_text("bad")
        with patch.object(host, "auth_service") as service:
            with self.assertRaises(host.OperationError):
                host.auth_restore_check(self.args, object())
            with self.assertRaises(host.OperationError):
                host.auth_remove(self.args, object())
            service.assert_not_called()

    def test_corrupt_json_status_and_backup_return_explicit_unknown_or_error(self):
        for payload in (b"broken", b"null", b"[]", b'{"probe":[]}'):
            output = io.StringIO()
            with patch.object(host, "auth_service", return_value=SimpleNamespace(returncode=20, stdout=payload)), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(host.auth_check(self.args, object()), 20)
            self.assertFalse(json.loads(output.getvalue())["probe"]["fresh"])
        state = host.auth_state(self.args)
        host.auth_backup(state, self.args, self.files())
        (state / "auth.json").write_text("[]")
        with self.assertRaises(host.OperationError):
            host.auth_read_backup(state, self.args)

    def test_cli_auth_arguments_route_to_auth_without_adb(self):
        for command, function in (("auth-install", "auth_install"), ("auth-check", "auth_check"),
                                  ("auth-remove", "auth_remove"), ("auth-restore-check", "auth_restore_check")):
            with patch.object(sys, "argv", ["host.py", command, "--serial", "fixture", "--source-oid",
                                            "b" * 40, "--state-dir", str(self.state),
                                            "--injector", "/private/injector", "--bundle", "/private/bundle"]), \
                    patch.object(host, function, return_value=0) as action:
                self.assertEqual(host.main(), 0)
                args, device = action.call_args.args
                self.assertEqual(args.injector, "/private/injector")
                self.assertEqual(args.bundle, "/private/bundle")
                self.assertEqual(device.serial, "fixture")


class AuthShellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.parent = self.root / "private"
        self.work = self.parent / "auth"
        self.golden = self.parent / "auth.recovery"
        self.boot = self.root / "service.d/auth.sh"
        self.proc = self.root / "proc"
        for directory in (self.parent, self.work, self.golden, self.boot.parent, self.proc):
            directory.mkdir(mode=0o700)
        files = {"frida-inject": b"fixture ELF", "auth-hook.bundle.js": b"fixture bundle",
                 "auth-service.sh": b"fixture service", "boot-entry": b"fixture boot",
                 "source_oid": b"a" * 40 + b"\n"}
        for name, content in files.items():
            path = self.golden / name
            path.write_bytes(content)
            path.chmod(0o700 if name in {"frida-inject", "auth-service.sh", "boot-entry"} else 0o600)
        manifest = "".join(hashlib.sha256(b).hexdigest() + "  " + n + "\n" for n, b in files.items())
        (self.golden / "manifest").write_text(manifest)
        (self.golden / "manifest.sha256").write_text(hashlib.sha256(manifest.encode()).hexdigest() + "\n")
        for name in ("manifest", "manifest.sha256"):
            (self.golden / name).chmod(0o600)
        self.script = (DIRECTORY / "auth-service.sh").read_text().removesuffix('main "$@"\n')
        self.script = self.script.replace("/data/adb/f50-soft-router", str(self.parent))
        self.script = self.script.replace("/data/adb/service.d/f50-auth-hook.sh", str(self.boot))
        self.script = self.script.replace("/proc/", str(self.proc) + "/")
        self.script = self.script.replace("/system/bin/getprop", "getprop")
        self.script = self.script.replace(host.AUTH_INJECTOR_SHA, hashlib.sha256(files["frida-inject"]).hexdigest())
        # Native I/O and hashes; only Linux stat spelling/root UID are simulated on macOS.
        helper = self.root / "tools.py"
        helper.write_text('''import fcntl, hashlib, os, sys
if sys.argv[1] == "stat":
    s = os.stat(sys.argv[-1]); fmt = sys.argv[3]
    for k, v in {"%u": "0", "%a": oct(s.st_mode & 0o777)[2:], "%i": str(s.st_ino),
                 "%s": str(s.st_size), "%Y": str(int(s.st_mtime)), "%Z": str(int(s.st_ctime))}.items():
        fmt = fmt.replace(k, v)
    print(fmt)
elif sys.argv[1] == "hash":
    data = open(sys.argv[2], "rb").read() if len(sys.argv) > 2 else sys.stdin.buffer.read()
    print(hashlib.sha256(data).hexdigest() + "  file")
elif sys.argv[1] == "flock":
    try: fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: sys.exit(1)
''')
        self.script += f'\nstat() {{ python3 {shlex.quote(str(helper))} stat "$@"; }}\n'
        self.script += f'sha256sum() {{ python3 {shlex.quote(str(helper))} hash "$@"; }}\n'
        busybox = self.root / "busybox"
        busybox.write_text('#!/bin/sh\nif [ "$1" = tar ]; then shift; exec tar "$@"; fi\n' +
                           f'exec python3 {shlex.quote(str(helper))} "$@"\n')
        busybox.chmod(0o700)
        self.script += f'BB={shlex.quote(str(busybox))}\nsleep() {{ :; }}\n'

    def shell(self, commands, expected=0):
        result = subprocess.run(["sh", "-c", self.script + "\n" + commands], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        return result.stdout.decode()

    def make_process(self, pid, start, command, executable=None):
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        (directory / "cmdline").write_bytes(command)
        # Fields 3..22: state, 18 placeholders, then starttime.
        (directory / "stat").write_text(f"{pid} (name with spaces) S " + "0 " * 18 + str(start) + "\n")
        if executable:
            (directory / "exe").symlink_to(executable)

    def test_heals_missing_corrupt_symlink_and_permissions_but_never_enabled(self):
        self.shell("heal")
        (self.work / "auth-hook.bundle.js").write_bytes(b"broken")
        (self.work / "auth-service.sh").unlink()
        outside = self.root / "outside"
        outside.write_bytes(b"leave this alone")
        self.boot.unlink()
        self.boot.symlink_to(outside)
        (self.work / "source_oid").chmod(0o644)
        self.shell("heal")
        self.assertEqual((self.work / "auth-hook.bundle.js").read_bytes(), b"fixture bundle")
        self.assertEqual((self.work / "source_oid").stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.boot.is_symlink())
        self.assertEqual(outside.read_bytes(), b"leave this alone")
        self.assertFalse((self.work / "enabled").exists())

    def test_bad_golden_or_manifest_never_rebaselines_from_work(self):
        self.shell("heal")
        (self.golden / "auth-hook.bundle.js").write_bytes(b"broken")
        (self.work / "auth-hook.bundle.js").write_bytes(b"different")
        self.shell("heal", 20)
        self.assertEqual((self.work / "auth-hook.bundle.js").read_bytes(), b"different")
        self.assertEqual((self.golden / "auth-hook.bundle.js").read_bytes(), b"broken")
        (self.golden / "manifest").write_text("bad manifest\n")
        self.shell("golden", 20)

    def test_host_backup_repairs_only_a_known_golden_manifest(self):
        files = {p.name: p.read_bytes() for p in self.golden.iterdir()}
        archive = self.root / "backup.tar"
        archive.write_bytes(host.auth_archive(files))
        expected = hashlib.sha256(files["manifest"]).hexdigest()
        (self.golden / "auth-hook.bundle.js").write_bytes(b"broken")
        command = f'start() {{ heal; }}; install {expected} < {shlex.quote(str(archive))}'
        self.shell(command)
        self.assertEqual((self.golden / "auth-hook.bundle.js").read_bytes(), files["auth-hook.bundle.js"])
        self.assertEqual((self.work / "auth-hook.bundle.js").read_bytes(), files["auth-hook.bundle.js"])
        (self.golden / "manifest").write_text("unknown identity\n")
        self.shell(command, 20)
        self.assertEqual((self.golden / "manifest").read_text(), "unknown identity\n")
        self.assertTrue((self.parent / "auth.recovery.stage").is_dir())

    def test_unsafe_status_path_is_not_followed_even_when_reporting_error(self):
        outside = self.root / "outside"
        outside.write_bytes(b"untouched")
        (self.work / "status.new").symlink_to(outside)
        self.shell("write_status runtime-unsafe", 20)
        self.assertEqual(outside.read_bytes(), b"untouched")

    def test_identity_rejects_pid_reuse_and_ambiguous_full_target_cmdline(self):
        self.make_process(123, 456, b"com.zte.web\0")
        identity = self.shell("identity 123").strip()
        self.assertEqual(self.shell("target").strip(), identity)
        self.make_process(123, 999, b"com.zte.web\0")
        self.shell(f"same {shlex.quote(identity)}", 1)
        self.shell(f"injector={shlex.quote(identity)}; natural_exit", 20)
        self.make_process(124, 12, b"com.zte.web\0extra\0")
        self.shell("target", 20)

    def test_target_accepts_android_nul_padding_but_rejects_extra_arguments_and_prefix(self):
        raw = b"com.zte.web" + b"\0" * 80
        self.make_process(123, 456, raw)
        identity = self.shell("target").strip()
        self.assertEqual(identity, f"123 456 {hashlib.sha256(raw).hexdigest()}")
        for command in (b"com.zte.web\0extra\0", b"com.zte.web\0\0extra\0", b"com.zte.web\n\0"):
            self.make_process(123, 456, command)
            self.shell("target", 20)
        for command in (b"com.zte.web.extra\0", b"com.zte.web-helper\0"):
            self.make_process(123, 456, command)
            self.shell("target", 10)
        self.make_process(123, 456, raw)
        self.make_process(124, 999, b"com.zte.web\0\0")
        self.shell("target", 20)

    def test_boot_and_missing_target_wait_without_spending_injection_budget(self):
        output = self.shell('''heal; : > "$A/enabled"; rounds=0; printf '%032d' 0 > "$A/check.request";
getprop() { [ "$1" = sys.boot_completed ] || exit 99; echo read >> "$P/boot-reads"; [ "$rounds" -ge 35 ] && echo 1 || echo 0; };
sleep() { rounds=$((rounds + 1)); [ "$rounds" -lt 65 ] || rm -f "$A/enabled"; };
run; echo "failures=$failures"''')
        self.assertIn("failures=0", output)
        self.assertFalse((self.work / "stdin").exists())
        self.assertFalse((self.work / "injector.identity").exists())
        self.assertEqual(len((self.parent / "boot-reads").read_text().splitlines()), 3)
        self.shell('getprop() { echo 0; }; target() { echo unexpected > "$A/target-called"; }; launch', 10)
        self.assertFalse((self.work / "target-called").exists())

    def test_install_remove_reinstall_new_source_and_restore_query_are_scoped(self):
        original = {p.name: p.read_bytes() for p in self.golden.iterdir()}
        first_sha = hashlib.sha256(original["manifest"]).hexdigest()
        self.shell(f'main remove {first_sha} {"a" * 40}')
        proxy = self.parent / "deployment.json"
        proxy.write_bytes(b"keep proxy deployment")
        backup = self.parent / "original-apk-backup"
        backup.mkdir()
        (backup / "base.apk").write_bytes(b"keep original")
        for oid in ("a" * 40, "b" * 40):
            files = {n: b for n, b in original.items() if n not in {"manifest", "manifest.sha256"}}
            files["source_oid"] = (oid + "\n").encode()
            files["manifest"] = "".join(hashlib.sha256(b).hexdigest() + "  " + n + "\n" for n, b in files.items()).encode()
            sha = hashlib.sha256(files["manifest"]).hexdigest()
            files["manifest.sha256"] = (sha + "\n").encode()
            archive = self.root / (oid[0] + ".tar")
            archive.write_bytes(host.auth_archive(files))
            self.shell(f'start() {{ heal; }}; main install {sha} < {shlex.quote(str(archive))}')
            self.assertEqual((self.work / "source_oid").read_text(), oid + "\n")
            self.shell("main restore-check", 20)
            self.shell("main check", 20)
            if oid.startswith("a"):
                self.shell(f'main remove {sha} {oid}')
                self.assertEqual(json.loads(self.shell("main check", 10))["state"], "uninstalled")
                self.shell("main restore-check")
                self.shell(f'main remove {sha} {oid}')
                self.assertFalse(self.work.exists())
                self.assertFalse(self.golden.exists())
                self.assertFalse(self.boot.exists())
            else:
                self.shell(f'main remove {first_sha} {"a" * 40}', 20)
                self.assertEqual((self.work / "source_oid").read_text(), oid + "\n")
            self.assertEqual(archive.read_bytes(), host.auth_archive(files))
        self.assertEqual(proxy.read_bytes(), b"keep proxy deployment")
        self.assertEqual((backup / "base.apk").read_bytes(), b"keep original")

    def test_cleanup_unknown_content_or_wrong_source_preserves_assets(self):
        self.shell("heal")
        expected = hashlib.sha256((self.golden / "manifest").read_bytes()).hexdigest()
        self.shell(f'main remove {expected} {"b" * 40}', 20)
        self.assertTrue(self.boot.exists())
        unknown = self.work / ".original-backup"
        unknown.mkdir()
        (unknown / "original").write_bytes(b"preserve")
        self.shell(f'main remove {expected} {"a" * 40}', 20)
        self.assertEqual((unknown / "original").read_bytes(), b"preserve")
        self.assertTrue((self.work / "auth-hook.bundle.js").exists())
        self.assertTrue((self.golden / "manifest").exists())

    def test_absence_requires_no_assets_stage_supervisor_or_injector_and_is_read_only(self):
        expected = hashlib.sha256((self.golden / "manifest").read_bytes()).hexdigest()
        self.shell(f'main remove {expected} {"a" * 40}')
        # Empty baseline with no lock files must not create any during queries.
        for path in (self.parent / "auth.lock", self.parent / "auth.control.lock"):
            if path.exists():
                path.unlink()
        before = {p: p.stat().st_mtime_ns for p in self.parent.iterdir()}
        self.assertEqual(json.loads(self.shell("main check", 10))["state"], "uninstalled")
        self.shell("main restore-check")
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in self.parent.iterdir()})
        for name in ("auth.control.lock", "auth.lock"):
            lock = self.parent / name
            lock.touch(mode=0o600)
            with lock.open("r") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.shell("main check", 20)
                self.shell("main restore-check", 20)
                self.shell(f'main remove {expected} {"a" * 40}', 20)
            lock.unlink()
        stage = self.parent / "auth.recovery.stage"
        stage.mkdir(mode=0o700)
        self.shell("main check", 20)
        self.shell("main restore-check", 20)
        stage.rmdir()
        self.make_process(333, 444, b"/private/frida-inject\0")
        self.shell("main check", 20)
        self.shell("main restore-check", 20)
        (self.proc / "333/cmdline").write_bytes(str(self.golden / "auth-service.sh").encode() + b"\0run\0")
        self.shell("main check", 20)
        self.shell("main restore-check", 20)

    def test_stale_wrong_session_wrong_pid_and_false_probes_are_rejected(self):
        self.shell("heal")
        self.make_process(123, 456, b"com.zte.web\0")
        self.make_process(333, 444, b"frida-inject\0", str(self.work / "frida-inject"))
        target_id = self.shell("identity 123").strip()
        injector = self.shell("identity 333").strip()
        prefix = f"target_id={shlex.quote(target_id)}; injector={shlex.quote(injector)}; session=new; request=fresh; "
        message = {"event": "probe-passed", "pid": 123, "active": True, "calls": 2,
                   "session": "new", "nonce": "fresh", "returned": False}
        for field, value in (("nonce", "stale"), ("session", "old"), ("pid", 321), ("returned", True)):
            (self.work / "session.log").write_text(json.dumps({**message, field: value}, separators=(",", ":")) + "\n")
            self.shell(prefix + "wait_event probe-passed true ',\"returned\":false'", 20)
        (self.work / "session.log").write_text(json.dumps(message, separators=(",", ":")) + "\n")
        self.shell(prefix + "wait_event probe-passed true ',\"returned\":false'")

    def test_other_injector_refused_and_duplicate_supervisor_cannot_enter(self):
        self.make_process(333, 444, b"/private/frida-inject\0")
        self.shell("injector=; no_other_injector", 20)
        lock = self.parent / "auth.lock"
        with lock.open("w") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.shell("run", 20)
        self.assertFalse((self.work / "supervisor.identity").exists())

    def test_manual_disable_never_launches_and_natural_failure_retries_are_bounded(self):
        self.shell("heal; launch() { echo launched > \"$A/launched\"; return 20; }; run")
        self.assertFalse((self.work / "launched").exists())
        self.assertEqual(json.loads((self.work / "status.json").read_text())["state"], "stopped")
        self.shell('heal; : > "$A/enabled"; launch() { echo attempt >> "$A/attempts"; return 20; }; run', 20)
        self.assertEqual((self.work / "attempts").read_text().splitlines(), ["attempt"] * 3)
        self.assertEqual(json.loads((self.work / "status.json").read_text())["state"], "retry-limit")

    def test_stop_revokes_enabled_even_if_golden_is_invalid(self):
        self.shell('heal; : > "$A/enabled"')
        (self.golden / "auth-hook.bundle.js").write_bytes(b"bad")
        self.shell("main stop", 20)
        self.assertFalse((self.work / "enabled").exists())

    def test_restore_failure_never_signals_injector_and_confirmation_precedes_term(self):
        self.shell("heal")
        prefix = '''injector='333 444 hash'; target_id='123 456 hash';
injector_alive() { return 0; }; same() { return 0; }; no_other_injector() { return 0; };
nonce_new() { echo fresh; }; kill() { echo "$*" > "$A/signal"; };
exec 7> "$A/input";
'''
        self.shell(prefix + 'wait_event() { return 20; }; detach', 20)
        self.assertFalse((self.work / "signal").exists())
        self.assertEqual((self.work / "input").read_text(), "restore fresh\n")
        self.shell(prefix + 'wait_event() { return 0; }; natural_exit() { injector=; return 0; }; detach')
        self.assertEqual((self.work / "signal").read_text(), "-TERM 333\n")

    def test_disk_healing_does_not_reinject_a_running_session(self):
        self.shell("heal")
        self.make_process(123, 456, b"com.zte.web\0")
        self.make_process(333, 444, b"frida-inject\0", str(self.work / "frida-inject"))
        injector = self.shell("identity 333").strip()
        target_id = self.shell("identity 123").strip()
        self.shell(f'''heal; : > "$A/enabled"; rounds=0;
launch() {{ injector={shlex.quote(injector)}; target_id={shlex.quote(target_id)}; echo attempt >> "$A/attempts"; }};
probe() {{ passed=true; }}; detach() {{ injector=; return 0; }};
nonce_new() {{ echo fixture; }};
sleep() {{ rounds=$((rounds + 1));
    [ "$rounds" -ne 2 ] || printf broken > "$A/auth-hook.bundle.js";
    [ "$rounds" -lt 35 ] || rm -f "$A/enabled";
}};
run''')
        self.assertEqual((self.work / "attempts").read_text().splitlines(), ["attempt"])
        self.assertEqual((self.work / "auth-hook.bundle.js").read_bytes(), b"fixture bundle")


if __name__ == "__main__":
    unittest.main()
