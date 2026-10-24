"""Shape checks on the GitHub workflows the fork's releases depend on.

Nothing runs these files until a tag is pushed, so a typo in a trigger is a
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


@unittest.skipIf(yaml is None, "PyYAML not installed")
class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.doc = _load("release.yml")

    def test_it_triggers_on_the_fork_tag_pattern(self):
        patterns = self.doc["on"]["push"]["tags"]
        self.assertEqual(patterns, ["v[0-9]*-pokke.*"])
        self.assertTrue(fnmatch.fnmatchcase("v3.3.0-pokke.5", patterns[0]))
        self.assertFalse(fnmatch.fnmatchcase("v3.3.0", patterns[0]))

    def test_it_can_be_started_by_hand_with_a_tag(self):
        self.assertIn("tag", self.doc["on"]["workflow_dispatch"]["inputs"])

    def test_two_releases_do_not_cancel_each_other(self):
        conc = self.doc["concurrency"]
        self.assertIn("github.ref", conc["group"])
        self.assertIs(conc["cancel-in-progress"], False)

    def test_the_build_is_the_existing_workflow(self):
        build = self.doc["jobs"]["build"]
        self.assertEqual(build["uses"], "./.github/workflows/main.yml")
        self.assertIn("tag", build["with"])
        self.assertEqual(build["permissions"], {"contents": "read"})

    def test_only_publish_can_write(self):
        publish = self.doc["jobs"]["publish"]
        self.assertEqual(publish["needs"], "build")
        self.assertEqual(publish["permissions"], {"contents": "write"})
        self.assertIn("refs/tags/", publish["if"])
        self.assertIn("GH_TOKEN", publish["env"])

    def test_publish_creates_a_release_from_the_existing_tag(self):
        steps = self.doc["jobs"]["publish"]["steps"]
        text = "\n".join(str(s.get("run", "")) for s in steps)
        self.assertIn("gh release create", text)
        self.assertIn("--verify-tag", text)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class BuildWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.doc = _load("main.yml")

    def test_it_still_builds_on_push_and_by_hand(self):
        self.assertIn("master", self.doc["on"]["push"]["branches"])
        self.assertIn("workflow_dispatch", self.doc["on"])

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
