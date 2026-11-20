from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest

from factory.config import git, write_json
from factory.service import repair_service, service_label, service_spec, stage_runtime, wait_for_ready


class FakeLaunchctl:
    def __init__(self, *, loaded=False, disabled=True, fail_bootstrap=0):
        self.loaded, self.disabled = loaded, disabled
        self.fail_bootstrap = fail_bootstrap
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert argv[0] == "launchctl" and kwargs["cwd"] == "/" and kwargs["timeout"] == 15
        action = argv[1]
        code, stdout = 0, ""
        if action == "print-disabled":
            stdout = '{ "dev.software-factory.buchos" => ' + str(self.disabled).lower() + ' }'
        elif action == "print":
            code = 0 if self.loaded else 3
        elif action == "enable":
            self.disabled = False
        elif action == "disable":
            self.disabled = True
        elif action == "bootout":
            code = 0 if self.loaded else 3
            self.loaded = False
        elif action == "bootstrap":
            if self.fail_bootstrap:
                self.fail_bootstrap -= 1
                code = 1
            else:
                self.loaded = True
        else:
            raise AssertionError(action)
        return subprocess.CompletedProcess(argv, code, stdout, "")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "Documents/product source"
        self.source.mkdir(parents=True)
        self.factory = self.source / "factory"
        self.factory.mkdir()
        (self.factory / "__init__.py").write_text('VALUE = "committed-runtime"\n')
        (self.factory / "__main__.py").write_text('from . import VALUE\nprint(VALUE)\n')
        for args in (["init", "-b", "main"], ["config", "user.name", "Factory Test"],
                     ["config", "user.email", "test@local.invalid"], ["add", "factory"], ["commit", "-m", "initial"]):
            subprocess.run(["git", "-C", str(self.source), *args], check=True, capture_output=True)
        self.revision = git(self.source, "rev-parse", "HEAD")
        self.home = self.root / "home"
        self.state = self.home / "Library/Application Support/SoftwareFactory/buchos"
        self.state.mkdir(parents=True)
        self.config = self.state / "config.json"
        write_json(self.config, {"state_dir": str(self.state), "repo": str(self.source),
                                 "runtime": "codex", "integration_policy": "documentation_only"})
        self.plist = self.home / "Library/LaunchAgents/dev.software-factory.buchos.plist"

    def repair(self, **kwargs):
        return repair_service(self.source, self.config, expected_revision=self.revision,
                              python=sys.executable, home=self.home, **kwargs)

    def old_definition(self):
        old = service_spec(self.source, self.config, sys.executable, home=self.home)
        old["WorkingDirectory"] = str(self.source)
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        content = plistlib.dumps(old)
        self.plist.write_bytes(content)
        return content

    def test_committed_artifact_excludes_private_untracked_and_dirty_source(self):
        (self.source / ".env").write_text("synthetic-secret")
        (self.factory / "private.sqlite3").write_text("synthetic-private-state")
        (self.factory / "untracked.py").write_text("raise RuntimeError('untracked')")
        (self.factory / "__init__.py").write_text('VALUE = "dirty-working-tree"\n')
        artifact = stage_runtime(self.source, self.state, self.revision)
        files = sorted(path.relative_to(artifact).as_posix() for path in artifact.rglob("*") if path.is_file())
        self.assertEqual(files, ["factory/__init__.py", "factory/__main__.py", "manifest.json"])
        self.assertEqual((artifact / "factory/__init__.py").read_text(), 'VALUE = "committed-runtime"\n')
        for path in artifact.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        moved = self.source.with_name("source unavailable")
        self.source.rename(moved)
        env = dict(os.environ, PYTHONPATH=str(artifact), PYTHONDONTWRITEBYTECODE="1")
        ran = subprocess.run([sys.executable, "-m", "factory"], cwd=artifact, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(ran.stdout.strip(), "committed-runtime")

    def test_staging_replay_validates_against_source_not_self_asserted_manifest(self):
        artifact = stage_runtime(self.source, self.state, self.revision)
        self.assertEqual(stage_runtime(self.source, self.state, self.revision), artifact)
        changed = b'VALUE = "tampered"\n'
        (artifact / "factory/__init__.py").write_bytes(changed)
        manifest = json.loads((artifact / "manifest.json").read_text())
        manifest["files"]["factory/__init__.py"] = hashlib.sha256(changed).hexdigest()
        write_json(artifact / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "runtime_artifact_source_mismatch"):
            stage_runtime(self.source, self.state, self.revision)

    def test_source_revision_and_symlink_modules_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "runtime_source_revision_mismatch"):
            stage_runtime(self.source, self.state, "a" * 40)
        (self.factory / "linked.py").symlink_to("__init__.py")
        subprocess.run(["git", "-C", str(self.source), "add", "factory/linked.py"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-m", "symlink"], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, "runtime_source_entry_invalid"):
            stage_runtime(self.source, self.state)
        self.assertEqual(list((self.state / "runtimes").iterdir()), [])

    def test_prepare_preserves_all_existing_state_and_service(self):
        old = self.old_definition()
        for name in ("execution.sqlite3", "tools/sentinel", "backups/sentinel"):
            path = self.state / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"synthetic-existing")
        original_config = self.config.read_bytes()
        runner = FakeLaunchctl()
        result = self.repair(runner=runner)
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.plist.read_bytes(), old)
        self.assertEqual(self.config.read_bytes(), original_config)
        for name in ("execution.sqlite3", "tools/sentinel", "backups/sentinel"):
            self.assertEqual((self.state / name).read_bytes(), b"synthetic-existing")
        spec = plistlib.loads(Path(result["candidate"]).read_bytes())
        self.assertNotIn("Documents", spec["WorkingDirectory"])
        self.assertEqual(spec["EnvironmentVariables"]["PYTHONPATH"], spec["WorkingDirectory"])
        self.assertEqual(spec["EnvironmentVariables"]["PYTHONDONTWRITEBYTECODE"], "1")

    def test_existing_live_supervisor_prevents_activation(self):
        old = self.old_definition()
        write_json(self.state / "status.json", {"pid": 123, "process_identity": "synthetic"})
        runner = FakeLaunchctl()
        with self.assertRaisesRegex(RuntimeError, "existing_supervisor_running"):
            self.repair(activate=True, runner=runner, process_verifier=lambda *_: True)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.plist.read_bytes(), old)

    def test_unowned_definition_never_touches_launchctl(self):
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(plistlib.dumps({"Label": "different-service", "ProgramArguments": []}))
        runner = FakeLaunchctl()
        with self.assertRaisesRegex(ValueError, "service_definition_not_owned"):
            self.repair(activate=True, runner=runner, process_verifier=lambda *_: False)
        self.assertEqual(runner.calls, [])

    def test_failed_readiness_restores_disabled_old_definition_without_restarting_it(self):
        old = self.old_definition()
        runner = FakeLaunchctl(loaded=False, disabled=True)
        def timeout(*args, **kwargs):
            raise RuntimeError("service_ready_not_observed")
        with self.assertRaisesRegex(RuntimeError, "service_ready_not_observed"):
            self.repair(activate=True, runner=runner, readiness=timeout, process_verifier=lambda *_: False)
        self.assertEqual(self.plist.read_bytes(), old)
        self.assertTrue(runner.disabled)
        self.assertFalse(runner.loaded)
        self.assertEqual(sum(call[1] == "bootstrap" for call in runner.calls), 1)
        journal = json.loads(next((self.state / "service").glob("activation-*.json")).read_text())
        self.assertEqual(journal["status"], "failed")
        self.assertTrue(journal["rollback_complete"])

    def test_bootstrap_failure_restores_previously_loaded_owned_service(self):
        old = self.old_definition()
        runner = FakeLaunchctl(loaded=True, disabled=False, fail_bootstrap=1)
        with self.assertRaisesRegex(RuntimeError, "service_bootstrap_failed"):
            self.repair(activate=True, runner=runner, process_verifier=lambda *_: False)
        self.assertEqual(self.plist.read_bytes(), old)
        self.assertTrue(runner.loaded)
        self.assertFalse(runner.disabled)

    def test_success_is_recorded_only_after_fresh_verified_readiness(self):
        self.old_definition()
        runner = FakeLaunchctl()
        old_id = "a" * 32
        write_json(self.state / "status.json", {"startup_id": old_id})
        def ready(state, **kwargs):
            self.assertEqual(Path(state).resolve(), self.state.resolve())
            self.assertEqual(kwargs["previous_startup_id"], old_id)
            self.assertGreater(kwargs["not_before"], 0)
            return {"pid": 456, "startup_id": "b" * 32}
        result = self.repair(activate=True, runner=runner, readiness=ready, process_verifier=lambda *_: False)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["startup_id"], "b" * 32)
        journal = json.loads(next((self.state / "service").glob("activation-*.json")).read_text())
        self.assertEqual(journal["status"], "ready")

    def test_readiness_rejects_stale_reused_startup_and_unready_heartbeats(self):
        now = time.time()
        valid = {"ready": True, "status": "running", "startup_id": "b" * 32,
                 "pid": 456, "process_identity": "identity", "started_at": now, "updated_at": now}
        for changes in ({"ready": False}, {"status": "starting"}, {"startup_id": "a" * 32},
                        {"started_at": now - 100}, {"updated_at": now - 100}, {"updated_at": now + 100}):
            with self.subTest(changes=changes):
                write_json(self.state / "status.json", {**valid, **changes})
                with self.assertRaisesRegex(RuntimeError, "service_ready_not_observed"):
                    wait_for_ready(self.state, not_before=now, previous_startup_id="a" * 32,
                                   timeout=0, process_verifier=lambda *_: True)
        write_json(self.state / "status.json", valid)
        with self.assertRaisesRegex(RuntimeError, "service_ready_not_observed"):
            wait_for_ready(self.state, not_before=now, timeout=0, process_verifier=lambda *_: False)
        self.assertEqual(wait_for_ready(self.state, not_before=now, timeout=0,
                                       process_verifier=lambda *_: True)["pid"], 456)

    def test_only_valid_owned_label_names_are_accepted(self):
        with self.assertRaises(ValueError):
            service_label("../../another-service")


if __name__ == "__main__":
    unittest.main()
