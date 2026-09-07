import unittest
from dataclasses import replace

from job_scout.models import Candidate


def candidate(description: str) -> Candidate:
    return Candidate(
        source="greenhouse",
        tenant="acme",
        source_id="1",
        company="Acme",
        title="Platform Engineer",
        location="Remote",
        description=description,
        employment_type="Contract",
        remote=True,
        posted_at="2026-09-05T12:00:00Z",
        canonical_url="https://jobs.test/1",
        apply_url="https://jobs.test/1/apply",
        raw={"untrusted": "payload"},
    )


class CandidateTests(unittest.TestCase):
    def test_rejects_non_string_required_text_fields(self):
        for field in (
            "source",
            "tenant",
            "source_id",
            "company",
            "title",
            "description",
            "canonical_url",
            "apply_url",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, f"{field} must be string"):
                    replace(candidate("Linux platform"), **{field: ["malformed"]})

    def test_rejects_non_string_optional_text_fields(self):
        for field in ("location", "employment_type", "posted_at"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, f"{field} must be string"):
                    replace(candidate("Linux platform"), **{field: ["malformed"]})

    def test_rejects_non_boolean_remote_state(self):
        with self.assertRaisesRegex(ValueError, "remote must be boolean"):
            replace(candidate("Linux platform"), remote="false")

    def test_tracking_only_url_changes_do_not_change_content_hash(self):
        first = candidate("Linux platform")
        tracked = replace(
            first,
            canonical_url=first.canonical_url + "?utm_source=board",
            apply_url=first.apply_url + "?source=campaign",
        )

        self.assertEqual(
            first.to_dict()["content_hash"], tracked.to_dict()["content_hash"]
        )

    def test_serialized_candidate_has_deterministic_content_hash(self):
        first = candidate("Linux platform")
        second = candidate("Linux platform")
        changed = candidate("Linux platform and Kubernetes")

        first_value = first.to_dict()
        second_value = second.to_dict()
        changed_value = changed.to_dict()

        self.assertEqual(first_value["content_hash"], second_value["content_hash"])
        self.assertNotEqual(first_value["content_hash"], changed_value["content_hash"])
        self.assertNotIn("raw", first_value)
