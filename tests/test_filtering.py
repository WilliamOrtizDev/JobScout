import unittest

from job_scout.filtering import prefilter
from job_scout.models import Candidate


def candidate(**overrides):
    values = {
        "source": "test",
        "tenant": "acme",
        "source_id": "1",
        "company": "Acme",
        "title": "Senior Platform Engineer",
        "location": "Remote, United States",
        "description": "Six month C2C contract maintaining Linux systems.",
        "employment_type": "Contract",
        "remote": True,
        "posted_at": None,
        "canonical_url": "https://example.test/jobs/1",
        "apply_url": "https://example.test/jobs/1/apply",
        "raw": {},
    }
    values.update(overrides)
    return Candidate(**values)


PROFILE = {
    "targets": {
        "titles": ["Linux Systems Engineer", "Platform Engineer", "DevOps Engineer"],
        "remote_only": True,
        "contract_only": True,
    }
}


class PrefilterTests(unittest.TestCase):
    def test_rejects_non_object_targets_configuration(self):
        with self.assertRaisesRegex(ValueError, "targets must be an object"):
            prefilter(candidate(), {"targets": []})

    def test_rejects_non_boolean_filter_flags(self):
        with self.assertRaisesRegex(ValueError, "remote_only must be boolean"):
            prefilter(candidate(), {"targets": {"remote_only": "false"}})

    def test_rejects_string_title_configuration(self):
        malformed = {"targets": {"titles": "Platform Engineer"}}

        with self.assertRaisesRegex(ValueError, "titles must be a list"):
            prefilter(candidate(), malformed)

    def test_keeps_matching_remote_contract(self):
        self.assertTrue(prefilter(candidate(), PROFILE).keep)

    def test_rejects_unrelated_title_before_model_processing(self):
        result = prefilter(candidate(title="Account Executive"), PROFILE)
        self.assertFalse(result.keep)
        self.assertIn("title", result.reasons)

    def test_does_not_broaden_information_security_officer_to_physical_security(self):
        profile = {
            "targets": {
                "titles": ["Information Systems Security Officer"],
                "remote_only": True,
                "contract_only": True,
            }
        }

        result = prefilter(candidate(title="Security Officer"), profile)

        self.assertFalse(result.keep)
        self.assertIn("title", result.reasons)

    def test_title_acronyms_match_whole_tokens_only(self):
        profile = {
            "targets": {
                "titles": ["ISSO"],
                "remote_only": True,
                "contract_only": True,
            }
        }

        self.assertTrue(prefilter(candidate(title="Senior ISSO"), profile).keep)
        result = prefilter(candidate(title="Business Consultant - Missouri"), profile)
        self.assertFalse(result.keep)
        self.assertIn("title", result.reasons)

    def test_explicit_non_remote_takes_precedence_over_location_text(self):
        result = prefilter(
            candidate(remote=False, location="Remote-friendly team in Dallas"), PROFILE
        )

        self.assertFalse(result.keep)
        self.assertIn("remote", result.reasons)

    def test_rejects_explicit_onsite_role(self):
        result = prefilter(candidate(remote=False, location="Dallas, Texas"), PROFILE)
        self.assertFalse(result.keep)
        self.assertIn("remote", result.reasons)

    def test_keeps_unknown_contract_type_for_later_verification(self):
        result = prefilter(candidate(employment_type=None, description="Linux operations"), PROFILE)
        self.assertTrue(result.keep)
        self.assertIn("contract_unknown", result.reasons)

    def test_rejects_full_time_when_contract_word_is_negated(self):
        result = prefilter(
            candidate(
                employment_type="Full-time",
                description="This is not a contract role. Linux operations.",
            ),
            PROFILE,
        )

        self.assertFalse(result.keep)
        self.assertIn("contract", result.reasons)

    def test_rejects_explicit_non_contract_employment_types(self):
        for employment_type in ("Internship", "Part-time"):
            with self.subTest(employment_type=employment_type):
                result = prefilter(
                    candidate(
                        employment_type=employment_type,
                        description="Linux operations role.",
                    ),
                    PROFILE,
                )
                self.assertFalse(result.keep)
                self.assertIn("contract", result.reasons)

    def test_accepts_configured_negotiable_contract_category(self):
        profile = {
            "targets": {
                "contract_only": True,
                "engagement_types_allowed": [
                    "1099",
                    "C2C",
                    "contract type unspecified or negotiable",
                ],
            }
        }

        self.assertTrue(prefilter(candidate(description="Contract role"), profile).keep)

    def test_keeps_partial_engagement_prohibition_for_later_verification(self):
        decision = prefilter(candidate(description="Contract role. No C2C."), PROFILE)

        self.assertTrue(decision.keep)

    def test_enforces_configured_engagement_types(self):
        profile = {
            "targets": {
                "titles": ["Platform Engineer"],
                "contract_only": True,
                "engagement_types_allowed": ["1099"],
            }
        }
        decision = prefilter(
            candidate(description="Contract role. C2C allowed; no 1099."), profile
        )

        self.assertFalse(decision.keep)
        self.assertIn("engagement", decision.reasons)

    def test_rejects_common_w2_only_and_contractor_exclusion_phrases(self):
        for description in (
            "W2 candidates only",
            "Only W2 candidates",
            "Contractors are not accepted",
        ):
            with self.subTest(description=description):
                decision = prefilter(
                    candidate(description=description, employment_type="Contract"), PROFILE
                )
                self.assertFalse(decision.keep)
                self.assertIn("engagement", decision.reasons)

    def test_rejects_when_both_engagement_types_are_negated(self):
        for description in (
            "C2C is not allowed; 1099 not accepted",
            "No C2C or 1099",
        ):
            with self.subTest(description=description):
                decision = prefilter(candidate(description=description), PROFILE)

                self.assertFalse(decision.keep)
                self.assertIn("engagement", decision.reasons)

    def test_keeps_affirmative_engagement_wording_with_linking_verb(self):
        descriptions = (
            "Contract engagement. C2C is allowed; no 1099.",
            "Contract engagement. 1099 is accepted; no C2C.",
        )
        for description in descriptions:
            with self.subTest(description=description):
                self.assertTrue(prefilter(candidate(description=description), PROFILE).keep)

    def test_keeps_c2c_when_only_1099_is_prohibited(self):
        result = prefilter(
            candidate(
                employment_type="Contract",
                description="Six month contract. C2C allowed; no 1099.",
            ),
            PROFILE,
        )
        self.assertTrue(result.keep)

    def test_rejects_explicit_w2_only_without_c2c_or_1099(self):
        result = prefilter(
            candidate(
                employment_type="Contract",
                description="Contract engagement. W2 only; no C2C or 1099.",
            ),
            PROFILE,
        )
        self.assertFalse(result.keep)
        self.assertIn("engagement", result.reasons)

    def test_rejects_w2_only_when_disclosed_in_title(self):
        result = prefilter(
            candidate(title="Platform Engineer (W2 ONLY)", description="Contract role"),
            PROFILE,
        )
        self.assertFalse(result.keep)
        self.assertIn("engagement", result.reasons)


if __name__ == "__main__":
    unittest.main()
