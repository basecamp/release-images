"""Offline tests for the promotion decisions in scripts/promote.py: python3 -m unittest -v"""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import promote  # noqa: E402

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
FAMILY = {"tags": ["ref", "version", "minor", "major", "latest"]}
KAMAL = {"tags": ["ref", "latest"]}


def decide(pkg, tag, digest, registry):
    """registry: {tag: digest} as the release package has it now."""
    return promote.decide_tags(pkg, tag, digest, set(registry), registry.get)


class WriteOnceVersionTags(unittest.TestCase):
    def test_new_version_writes_its_exact_tags(self):
        write, skip = decide(FAMILY, "v1.5.3", D1, {"v1.5.2": D2, "1.5.2": D2})
        self.assertEqual(skip, [])
        self.assertIn("v1.5.3", write)
        self.assertIn("1.5.3", write)

    def test_existing_tag_with_same_digest_is_skipped(self):
        write, skip = decide(FAMILY, "v1.5.3", D1, {"v1.5.3": D1, "1.5.3": D1, "latest": D1, "1.5": D1, "1": D1})
        self.assertEqual(skip, ["v1.5.3", "1.5.3"])
        self.assertNotIn("v1.5.3", write)
        self.assertNotIn("1.5.3", write)

    def test_partial_rerun_writes_only_what_is_missing(self):
        write, skip = decide(FAMILY, "v1.5.3", D1, {"v1.5.3": D1})
        self.assertEqual(skip, ["v1.5.3"])
        self.assertIn("1.5.3", write)

    def test_existing_tag_with_other_digest_refuses(self):
        with self.assertRaises(promote.Refused) as e:
            decide(FAMILY, "v1.5.3", D1, {"v1.5.3": D2})
        self.assertIn(D2, str(e.exception))
        self.assertIn("v1.5.3", str(e.exception))

    def test_existing_version_alias_with_other_digest_refuses(self):
        with self.assertRaises(promote.Refused):
            decide(FAMILY, "v1.5.3", D1, {"1.5.3": D2})

    def test_refuses_before_writing_anything(self):
        # A conflict on the second write-once tag must stop the run, not leave the first written.
        with self.assertRaises(promote.Refused):
            decide(FAMILY, "v1.5.3", D1, {"v1.5.3": D1, "1.5.3": D2})


class LatestNeverRegresses(unittest.TestCase):
    def test_highest_release_moves_latest(self):
        write, _ = decide(KAMAL, "v2.13.0", D1, {"v2.12.0": D2, "latest": D2})
        self.assertIn("latest", write)

    def test_older_release_does_not_move_latest(self):
        write, _ = decide(KAMAL, "v1.9.4", D1, {"v2.12.0": D2, "latest": D2})
        self.assertEqual(write, ["v1.9.4"])

    def test_older_release_approved_after_newer_does_not_move_latest(self):
        write, _ = decide(FAMILY, "v1.5.3", D1, {"v1.5.4": D2, "1.5.4": D2, "latest": D2, "1.5": D2, "1": D2})
        self.assertNotIn("latest", write)
        self.assertNotIn("1.5", write)
        self.assertNotIn("1", write)

    def test_prerelease_never_moves_latest(self):
        for tag in ("v2.13.0-rc.1", "v2.13.0.beta1"):
            write, _ = decide(KAMAL, tag, D1, {"v2.12.0": D2, "latest": D2})
            self.assertEqual(write, [tag])

    def test_prereleases_do_not_hold_latest_back(self):
        write, _ = decide(KAMAL, "v2.12.1", D1, {"v2.12.0": D2, "v2.13.0.rc1": D2, "latest": D2})
        self.assertIn("latest", write)

    def test_first_release_moves_latest(self):
        write, _ = decide(FAMILY, "v0.1.0", D1, {"main": D2, "latest": D2})
        self.assertEqual(write, ["v0.1.0", "0.1.0", "0.1", "0", "latest"])

    def test_series_tags_move_forward_within_their_series_only(self):
        write, _ = decide(FAMILY, "v1.4.10", D1, {"v1.5.2": D2, "v1.4.9": D2})
        self.assertIn("1.4", write)
        self.assertNotIn("1", write)
        self.assertNotIn("latest", write)

    def test_non_release_tag_refuses(self):
        for tag in ("vtest", "v1.2.3.4", "1.2.3", "v1.2.3;rm"):
            with self.assertRaises(promote.Refused):
                decide(FAMILY, tag, D1, {})


class PromotionRecord(unittest.TestCase):
    def test_record_names_what_was_promoted_and_who_approved(self):
        pkg = {"source": "basecamp/kamal", "workflow": "docker-publish.yml",
               "edge": "ghcr.io/basecamp/kamal-edge", "release": "ghcr.io/basecamp/kamal", "tags": ["ref", "latest"]}
        p = promote.promotion_predicate(
            name="kamal", pkg=pkg, tag="v2.13.0", digest=D1, commit="a" * 40, approvers=["djmb"],
            tags=["v2.13.0", "latest"], run_url="https://github.com/basecamp/release-images/actions/runs/1",
            builder="https://github.com/basecamp/release-images/.github/workflows/promote.yml@refs/heads/main")
        ext = p["buildDefinition"]["externalParameters"]
        self.assertEqual(ext["package"], "kamal")
        self.assertEqual(ext["tag"], "v2.13.0")
        self.assertEqual(ext["digest"], D1)
        self.assertEqual(ext["source"]["repository"], "basecamp/kamal")
        self.assertEqual(ext["source"]["commit"], "a" * 40)
        self.assertEqual(ext["approvers"], ["djmb"])
        self.assertEqual(ext["tags"], ["v2.13.0", "latest"])
        deps = {d["uri"]: d["digest"] for d in p["buildDefinition"]["resolvedDependencies"]}
        self.assertEqual(deps["git+https://github.com/basecamp/kamal@refs/tags/v2.13.0"], {"gitCommit": "a" * 40})
        self.assertEqual(deps["oci://ghcr.io/basecamp/kamal-edge"], {"sha256": "1" * 64})
        self.assertTrue(p["runDetails"]["builder"]["id"].endswith("promote.yml@refs/heads/main"))


if __name__ == "__main__":
    unittest.main()
