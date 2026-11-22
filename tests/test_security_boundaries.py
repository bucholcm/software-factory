import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from factory.adapters import documentation_path, is_documentation_change
from factory.config import command, git, write_json
from factory.integration import integrate_ready
from factory.recovery import code_checkpoint, restore_code_into
from factory.release import create_checkpoint, restore_into
from factory.scope import safe_path
from factory.store import Store


class SecurityBoundariesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.raw_git("init", "-b", "main")
        self.raw_git("config", "user.name", "Synthetic")
        self.raw_git("config", "user.email", "test@local.invalid")
        (self.repo / "README.md").write_text("synthetic\n")
        self.raw_git("add", "README.md")
        self.raw_git("commit", "-m", "initial")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def raw_git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def test_executable_agent_templates_do_not_qualify_as_documentation(self):
        for name in (".claude/rules/review.md", ".cursor/rules/review.md", "prompts/review.md",
                     "integrations/opencode/commands/factory.md", "docs/prompts/review.md", "src/template.md"):
            with self.subTest(name=name):
                self.assertFalse(documentation_path(name))
        self.assertTrue(documentation_path("README.md"))
        self.assertTrue(documentation_path("docs/usage.md"))
        path = self.repo / "integrations/opencode/commands/factory.md"
        path.parent.mkdir(parents=True)
        path.write_text("synthetic command\n")
        self.raw_git("add", ".")
        self.raw_git("commit", "-m", "command")
        self.assertFalse(is_documentation_change(self.repo, self.base))

    def test_factory_git_does_not_execute_configured_checkout_hooks(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        marker = self.root / "hook-ran"
        hook = hooks / "post-checkout"
        hook.write_text("#!/bin/sh\nprintf synthetic > " + shlex.quote(str(marker)) + "\n")
        hook.chmod(0o700)
        self.raw_git("config", "core.hooksPath", str(hooks))
        result = command(["git", "worktree", "add", "-b", "factory/test", str(self.root / "worktree")], cwd=self.repo)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        # Control: the same armed hook runs for a normal, user-owned Git command.
        self.raw_git("checkout", "main")
        self.assertTrue(marker.exists())

    def test_code_checkpoint_rejects_manifest_and_bundle_symlinks(self):
        checkpoint = code_checkpoint(self.repo, self.root / "backups")
        for name in ("manifest.json", "code.bundle"):
            with self.subTest(name=name):
                original = checkpoint / name
                external = self.root / name
                original.rename(external)
                original.symlink_to(external)
                with self.assertRaisesRegex(ValueError, "checkpoint_invalid"):
                    restore_code_into(checkpoint, self.root / "restored")
                self.assertFalse((self.root / "restored").exists())
                original.unlink()
                external.rename(original)

    def test_code_restore_refuses_option_shaped_revision_before_git_runs(self):
        checkpoint = code_checkpoint(self.repo, self.root / "backups")
        manifest = json.loads((checkpoint / "manifest.json").read_text())
        manifest["base_revision"] = "--help"
        write_json(checkpoint / "manifest.json", manifest)
        with patch("factory.recovery.command") as runner:
            with self.assertRaisesRegex(ValueError, "checkpoint_not_verified"):
                restore_code_into(checkpoint, self.root / "restored")
        runner.assert_not_called()

    def test_state_restore_rejects_internal_parent_symlinks(self):
        mutable = self.root / "synthetic.json"
        mutable.write_text('{"synthetic": true}')
        inventory = [{"name": "candidate", "kind": "file", "path": str(mutable)}]
        for folder in ("code", "state"):
            with self.subTest(folder=folder):
                checkpoint = create_checkpoint(self.repo, self.root / "backups", inventory)
                original = checkpoint / folder
                external = self.root / ("external-" + folder)
                original.rename(external)
                original.symlink_to(external, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "checkpoint_invalid"):
                    restore_into(checkpoint, self.root / "restored")
                self.assertFalse((self.root / "restored").exists())

    def test_private_credential_paths_and_control_characters_cannot_enter_scope(self):
        for name in (".aws/credentials", ".ssh/id_rsa", ".npmrc", "src/a\nb.py", "src/a\tb.py"):
            with self.subTest(name=name):
                self.assertFalse(safe_path(name))

    def test_dynamic_sql_identifiers_are_allowlisted_and_values_are_bound(self):
        store = Store(self.root / "store")
        job = store.enqueue("synthetic", self.repo, "factory/test")
        with self.assertRaisesRegex(ValueError, "invalid_job_fields"):
            store.update(job["id"], **{"stage='integrated' --": "bad"})
        value = "'; DROP TABLE jobs; --"
        store.update(job["id"], blocker=value)
        self.assertEqual(len(store.jobs()), 1)
        self.assertEqual(store.get(job["id"])["blocker"], value)

    def test_integration_recovery_revalidates_the_recorded_backup(self):
        store = Store(self.root / "store")
        worktree = self.root / "worktree"
        git(self.repo, "worktree", "add", "-b", "factory/test", str(worktree))
        (worktree / "README.md").write_text("synthetic change\n")
        git(worktree, "commit", "-am", "documentation")
        revision = git(worktree, "rev-parse", "HEAD")
        job = store.enqueue("synthetic", worktree, "factory/test", payload={"base_revision": self.base})
        store.update(job["id"], stage="ready", revision=revision, reviewed_revision=revision,
                     review_result="approved", tests_passed=True)
        config = {"repo": str(self.repo), "state_dir": str(store.state_dir), "base_branch": "main",
                  "auto_merge": True, "integration_policy": "documentation_only"}
        supervisor = type("Supervisor", (), {"store": store})()
        integrate_ready(supervisor, config)
        journal = json.loads((store.state_dir / "integrations" / (job["id"] + ".json")).read_text())
        bundle = store.state_dir / "backups" / journal["checkpoint_id"] / "code.bundle"
        with bundle.open("ab") as stream:
            stream.write(b"synthetic-tampering")
        store.update(job["id"], stage="ready")
        with self.assertRaisesRegex(ValueError, "checkpoint_not_verified"):
            integrate_ready(supervisor, config)
        self.assertEqual(store.get(job["id"])["stage"], "ready")
