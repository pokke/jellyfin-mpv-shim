"""Shape checks on the GitHub workflows the fork's releases depend on.

Nothing runs these files until master is pushed, so a typo in a trigger is a
release that silently never happens. Skipped where PyYAML is not installed.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import fnmatch
import os
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

WORKFLOWS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".github", "workflows")


def _load(name):
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    # YAML 1.1 reads a bare `on` as the boolean True.
    doc["on"] = doc.get("on", doc.get(True))
    return doc


def _run_text(doc, job):
    return "\n".join(str(s.get("run", "")) for s in doc["jobs"][job]["steps"])


@unittest.skipIf(yaml is None, "PyYAML not installed")
class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.doc = _load("release.yml")

    def test_every_push_to_master_releases(self):
        self.assertEqual(self.doc["on"]["push"]["branches"], ["master"])

    def test_there_is_no_tag_trigger(self):
        """Tags are made by the publish step; a tag trigger would loop."""
        self.assertNotIn("tags", self.doc["on"]["push"])

    def test_docs_tests_and_markdown_make_no_release(self):
        ignore = self.doc["on"]["push"]["paths-ignore"]
        self.assertEqual(sorted(ignore), ["**.md", "docs/**", "tests/**"])

        # GitHub skips a push only when EVERY changed file matches, so a push
        # touching code and docs still releases. fnmatch's "*" also crosses
        # "/", as GitHub's "**" does, which is all these patterns need.
        def skipped(files):
            return all(any(fnmatch.fnmatchcase(f, pat) for pat in ignore)
                       for f in files)

        self.assertTrue(skipped(["docs/packaging.md"]))
        self.assertTrue(skipped(["tests/test_x.py", "README.md"]))
        self.assertTrue(skipped(["jellyfin_mpv_shim/mpvtk/GUIDE.md"]))
        self.assertFalse(skipped(["jellyfin_mpv_shim/player.py"]))
        self.assertFalse(skipped(["jellyfin_mpv_shim/player.py",
                                  "docs/packaging.md"]))

    def test_it_can_be_started_by_hand(self):
        self.assertIn("workflow_dispatch", self.doc["on"])

    def test_two_releases_queue_instead_of_cancelling(self):
        conc = self.doc["concurrency"]
        self.assertEqual(conc["group"], "release")
        self.assertIs(conc["cancel-in-progress"], False)

    def test_the_tag_is_made_from_constants_and_the_run_number(self):
        prepare = self.doc["jobs"]["prepare"]
        self.assertIn("CLIENT_VERSION", _run_text(self.doc, "prepare"))
        self.assertIn("github.run_number", str(prepare))
        self.assertIn("-pokke.", str(prepare))
        self.assertIn("tag", prepare["outputs"])

    def test_the_build_is_the_existing_workflow_with_that_tag(self):
        build = self.doc["jobs"]["build"]
        self.assertEqual(build["uses"], "./.github/workflows/main.yml")
        self.assertIn("prepare", build["needs"])
        self.assertIn("needs.prepare.outputs.tag", build["with"]["tag"])
        self.assertEqual(build["permissions"], {"contents": "read"})

    def test_the_unit_suite_runs_without_write_access(self):
        test = self.doc["jobs"]["test"]
        self.assertEqual(test["permissions"], {"contents": "read"})
        self.assertIn("run_tests_parallel.py", _run_text(self.doc, "test"))

    def test_publish_waits_for_the_build_and_the_tests(self):
        publish = self.doc["jobs"]["publish"]
        self.assertEqual(sorted(publish["needs"]),
                         ["build", "prepare", "test"])
        self.assertEqual(publish["permissions"], {"contents": "write"})
        self.assertIn("GH_TOKEN", publish["env"])

    def test_only_a_push_to_master_of_the_fork_publishes(self):
        condition = self.doc["jobs"]["publish"]["if"]
        self.assertIn("github.event_name == 'push'", condition)
        self.assertIn("github.ref == 'refs/heads/master'", condition)
        self.assertIn("github.repository == 'pokke/jellyfin-mpv-shim'",
                      condition)

    def test_only_publish_can_write(self):
        for name, job in self.doc["jobs"].items():
            if name != "publish":
                self.assertNotEqual(
                    job.get("permissions", {}).get("contents"), "write", name)

    def test_publish_pins_the_release_to_the_built_commit(self):
        text = _run_text(self.doc, "publish")
        self.assertIn("gh release create", text)
        self.assertIn('--target "$GITHUB_SHA"', text)
        self.assertIn("--generate-notes", text)
        self.assertIn("--latest", text)
        self.assertNotIn("--verify-tag", text)

    def test_a_rerun_of_a_failed_publish_repairs_the_release(self):
        text = _run_text(self.doc, "publish")
        self.assertIn('gh release view "$TAG"', text)
        self.assertIn("gh release upload", text)
        self.assertIn("--clobber", text)
        self.assertIn("--draft=false", text)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class BuildWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.doc = _load("main.yml")

    def test_it_still_builds_on_push_and_by_hand(self):
        self.assertIn("main", self.doc["on"]["push"]["branches"])
        self.assertIn("working", self.doc["on"]["push"]["branches"])
        self.assertIn("workflow_dispatch", self.doc["on"])

    def test_master_is_built_by_release_yml_not_twice(self):
        self.assertNotIn("master", self.doc["on"]["push"]["branches"])

    def test_it_can_be_called_with_a_tag(self):
        call = self.doc["on"]["workflow_call"]
        self.assertFalse(call["inputs"]["tag"].get("required", False))

    def test_every_job_sees_the_build_identity(self):
        self.assertIn("JMS_FORK_BUILD", self.doc["env"])

    def test_the_release_downloads_an_artifact_that_exists(self):
        """release.yml names the Windows artifact; main.yml must upload it."""
        names = [step["with"]["name"]
                 for job in self.doc["jobs"].values()
                 for step in job["steps"]
                 if str(step.get("uses", "")).startswith(
                     "actions/upload-artifact")]
        self.assertIn("windows", names)
        publish = _load("release.yml")["jobs"]["publish"]["steps"]
        wanted = [s["with"]["name"] for s in publish
                  if str(s.get("uses", "")).startswith(
                      "actions/download-artifact")]
        self.assertTrue(wanted, "publish downloads nothing")
        for name in wanted:
            self.assertIn(name, names)


if __name__ == "__main__":
    unittest.main()
