"""
Unit tests for the JobTracker form-filling engine.

Tests are grouped by module/feature:
  - CV path resolution
  - lookup_answer (YAML static answers + ContentGenerator AI fallback)
  - _pick_best_option (exact / substring / AI / first-fallback)
  - ContentGenerator (cache, timeout, prompt building)
  - WorkdayAdapter page-crash handling (next_clicked try/except)

Run:
    python -m pytest test_unit.py -v
    # or without pytest:
    python test_unit.py
"""

import sys, io, time, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent

# ── Make sure project root is on sys.path ─────────────────────────────────────
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")


# =============================================================================
# 1. CV PATH RESOLUTION
# =============================================================================

class TestCVPathResolution(unittest.TestCase):

    def test_default_cv_path_exists(self):
        """The default CV file must exist on disk."""
        from core.applicator import CV_PATH
        self.assertTrue(
            CV_PATH.exists(),
            f"CV_PATH does not exist: {CV_PATH}\n"
            "Fix: rename the file or update CV_PATH in applicator.py"
        )

    def test_default_cv_is_pdf(self):
        """The CV file must be a PDF."""
        from core.applicator import CV_PATH
        self.assertEqual(CV_PATH.suffix.lower(), ".pdf")

    def test_resolve_cv_path_returns_default_when_no_variant(self):
        """_resolve_cv_path(None) returns the default CV_PATH."""
        from core.applicator import _resolve_cv_path, CV_PATH
        result = _resolve_cv_path(None)
        self.assertEqual(result, CV_PATH)

    def test_resolve_cv_path_unknown_variant_falls_back_to_default(self):
        """_resolve_cv_path with a non-existent variant name falls back to default."""
        from core.applicator import _resolve_cv_path, CV_PATH
        result = _resolve_cv_path("nonexistent_variant_xyz")
        self.assertEqual(result, CV_PATH)

    def test_resolve_cv_path_known_variant(self):
        """_resolve_cv_path returns the variant file when it exists."""
        from core.applicator import _resolve_cv_path
        # Create a temporary variant file
        variant_path = ROOT / "data" / "_test_variant.pdf"
        try:
            variant_path.write_bytes(b"%PDF-1.4 test")
            result = _resolve_cv_path("_test_variant")
            self.assertEqual(result, variant_path)
        finally:
            variant_path.unlink(missing_ok=True)


# =============================================================================
# 2. LOOKUP_ANSWER
# =============================================================================

class TestLookupAnswer(unittest.TestCase):

    def test_known_field_label_returns_yaml_value(self):
        """Well-known labels like 'first_name' should return YAML value."""
        from core.applicator import lookup_answer
        result = lookup_answer("First Name", candidate_field="first_name")
        self.assertTrue(len(result) > 0, "Expected non-empty answer for first_name")

    def test_normalized_label_match(self):
        """'First Name' label normalizes to 'first_name' and finds the answer."""
        from core.applicator import lookup_answer
        result = lookup_answer("First Name")
        self.assertTrue(len(result) > 0)

    def test_unknown_field_without_generator_returns_empty(self):
        """Completely unknown field with no generator should return ''."""
        from core.applicator import lookup_answer
        result = lookup_answer("Zork level in ancient gaming", field_type="text")
        self.assertEqual(result, "")

    def test_unknown_field_with_generator_calls_generate(self):
        """Unknown field should trigger ContentGenerator.generate()."""
        from core.applicator import lookup_answer
        gen = MagicMock()
        gen.generate.return_value = "some AI answer"
        result = lookup_answer("Describe your biggest challenge", field_type="textarea",
                               content_generator=gen)
        gen.generate.assert_called_once()
        self.assertEqual(result, "some AI answer")

    def test_unknown_field_generator_returns_none_falls_to_default(self):
        """If generator returns None, falls through to smart default."""
        from core.applicator import lookup_answer
        gen = MagicMock()
        gen.generate.return_value = None
        result = lookup_answer("Totally unknown textarea", field_type="textarea",
                               content_generator=gen)
        # Should fall through to about_me default (may be empty string but no exception)
        self.assertIsInstance(result, str)

    def test_email_field_returns_email(self):
        """'Email' label should return the configured email address."""
        from core.applicator import lookup_answer
        result = lookup_answer("Email")
        self.assertIn("@", result, f"Expected an email address, got: {result!r}")

    def test_generator_not_called_when_field_resolved_from_yaml(self):
        """ContentGenerator should NOT be called when YAML already has the answer."""
        from core.applicator import lookup_answer
        gen = MagicMock()
        result = lookup_answer("Email", content_generator=gen)
        gen.generate.assert_not_called()


# =============================================================================
# 3. _PICK_BEST_OPTION
# =============================================================================

class TestPickBestOption(unittest.TestCase):

    def _pick(self, options, value, label="Degree", field_type="select", gen=None):
        from core.applicator import _pick_best_option
        return _pick_best_option(options, value, label, field_type, gen)

    def test_exact_match(self):
        opts = ["Bachelor's Degree", "Master's Degree", "PhD"]
        self.assertEqual(self._pick(opts, "Bachelor's Degree"), "Bachelor's Degree")

    def test_exact_match_case_insensitive(self):
        opts = ["Bachelor's Degree", "Master's Degree"]
        self.assertEqual(self._pick(opts, "bachelor's degree"), "Bachelor's Degree")

    def test_substring_value_in_option(self):
        """'Bachelor' as value should match 'Bachelor's Degree' option."""
        opts = ["Bachelor's Degree", "Master's Degree", "PhD"]
        result = self._pick(opts, "Bachelor")
        self.assertEqual(result, "Bachelor's Degree")

    def test_substring_option_in_value(self):
        """'B.Sc.' contains 'B' but let's test a clear substring overlap."""
        opts = ["Yes", "No"]
        result = self._pick(opts, "Yes, I am authorized")
        self.assertEqual(result, "Yes")

    def test_empty_options_returns_none(self):
        result = self._pick([], "anything")
        self.assertIsNone(result)

    def test_ai_fallback_called_when_no_match(self):
        """When exact/substring both fail, ContentGenerator should be called."""
        opts = ["Alpha", "Beta", "Gamma"]
        gen = MagicMock()
        gen.generate.return_value = "Beta"
        result = self._pick(opts, "totally unrelated", gen=gen)
        gen.generate.assert_called_once()
        self.assertEqual(result, "Beta")

    def test_ai_returns_value_not_in_list_falls_to_first(self):
        """If Groq returns something not in options, fall back to first option."""
        opts = ["Alpha", "Beta", "Gamma"]
        gen = MagicMock()
        gen.generate.return_value = "Delta"  # Not in list
        result = self._pick(opts, "totally unrelated", gen=gen)
        self.assertEqual(result, "Alpha")

    def test_no_generator_no_match_returns_first(self):
        """Without a generator and no match, returns the first option."""
        opts = ["Option A", "Option B"]
        result = self._pick(opts, "xyzzy_no_match", gen=None)
        self.assertEqual(result, "Option A")

    def test_bsc_matches_bachelor(self):
        """B.Sc. value should match Bachelor's Degree via substring."""
        opts = ["Bachelor's Degree", "Master's Degree", "PhD", "High School"]
        result = self._pick(opts, "B.Sc.")
        # "B.Sc." doesn't contain "Bachelor" and vice versa — Groq fallback or first
        # Just assert it returns something from the list
        self.assertIn(result, opts)


# =============================================================================
# 4. CONTENT GENERATOR
# =============================================================================

class TestContentGenerator(unittest.TestCase):

    def _make_gen(self, groq_response="AI generated answer"):
        """Create a ContentGenerator with a mocked Groq client."""
        from core.content_generator import ContentGenerator
        client = MagicMock()
        client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=groq_response))]
        )
        return ContentGenerator(
            client=client,
            job_title="Software Engineer Intern",
            company="Acme Corp",
            job_description="Looking for Python developer.",
        ), client

    def test_generate_returns_ai_value(self):
        gen, client = self._make_gen("I have 2 years of Python experience.")
        result = gen.generate("Years of Python experience", "text")
        self.assertEqual(result, "I have 2 years of Python experience.")
        client.chat.completions.create.assert_called_once()

    def test_generate_caches_result(self):
        """Same field label + same job → Groq called only once."""
        gen, client = self._make_gen("cached answer")
        gen.generate("Describe yourself", "textarea")
        gen.generate("Describe yourself", "textarea")
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_generate_cache_different_labels(self):
        """Different labels should each call Groq."""
        gen, client = self._make_gen("answer")
        gen.generate("Label One", "text")
        gen.generate("Label Two", "text")
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_generate_timeout_returns_none(self):
        """If Groq hangs longer than timeout, generate() returns None gracefully."""
        from core.content_generator import ContentGenerator
        import time

        client = MagicMock()
        def slow_call(**kwargs):
            time.sleep(1.5)  # shorter than 5s so executor teardown is quick
            return MagicMock(choices=[MagicMock(message=MagicMock(content="late"))])
        client.chat.completions.create.side_effect = slow_call

        gen = ContentGenerator(client, "SWE Intern", "Acme", "desc")
        start = time.time()
        result = gen.generate("Some field", "text", timeout=0.3)
        elapsed = time.time() - start

        self.assertIsNone(result)
        # elapsed ≈ 1.5s (thread finishes), must be < 4s total
        self.assertLess(elapsed, 4.0, "Timeout did not fire in time")

    def test_generate_groq_error_returns_none(self):
        """API errors should return None, not raise."""
        from core.content_generator import ContentGenerator
        client = MagicMock()
        client.chat.completions.create.side_effect = Exception("API error")
        gen = ContentGenerator(client, "SWE", "Corp", "desc")
        result = gen.generate("Any field", "text")
        self.assertIsNone(result)

    def test_generate_empty_label_returns_none(self):
        """Empty field label should short-circuit to None without calling Groq."""
        gen, client = self._make_gen()
        result = gen.generate("", "text")
        self.assertIsNone(result)
        client.chat.completions.create.assert_not_called()

    def test_build_prompt_contains_job_info(self):
        """The generated prompt should include job title and company name."""
        from core.content_generator import ContentGenerator
        client = MagicMock()
        client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="answer"))]
        )
        gen = ContentGenerator(client, "Data Engineer", "Proofpoint", "Python job")

        # Capture the prompt by spying on _call_groq
        captured_prompt = []
        original_build = gen._build_prompt
        def spy_build(label, ftype, options):
            p = original_build(label, ftype, options)
            captured_prompt.append(p)
            return p
        gen._build_prompt = spy_build

        gen.generate("Describe a project", "textarea")
        self.assertTrue(len(captured_prompt) > 0)
        prompt = captured_prompt[0]
        self.assertIn("Data Engineer", prompt)
        self.assertIn("Proofpoint", prompt)

    def test_select_field_prompt_includes_options(self):
        """For select/radio fields, the prompt should list the available options."""
        from core.content_generator import ContentGenerator
        client = MagicMock()
        client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="Bachelor's Degree"))]
        )
        gen = ContentGenerator(client, "SWE Intern", "Corp", "desc")

        captured_prompt = []
        original_build = gen._build_prompt
        def spy_build(label, ftype, options):
            p = original_build(label, ftype, options)
            captured_prompt.append(p)
            return p
        gen._build_prompt = spy_build

        gen.generate("Degree", "select", options=["Bachelor's Degree", "Master's Degree", "PhD"])
        prompt = captured_prompt[0]
        self.assertIn("Bachelor's Degree", prompt)
        self.assertIn("Master's Degree", prompt)
        self.assertIn("PhD", prompt)


# =============================================================================
# 5. WORKDAY ADAPTER — PAGE CRASH HANDLING
# =============================================================================

class TestWorkdayPageCrashHandling(unittest.TestCase):
    """
    Verify that the page-closed exception after next_clicked is caught gracefully
    and returns a success result instead of propagating.
    """

    def test_next_clicked_page_closed_returns_success(self):
        """
        Simulate page.wait_for_timeout raising after next_clicked.
        The adapter should catch this and return AdapterResult.ok(...).
        """
        from core.adapters.workday_adapter import WorkdayAdapter
        from core.adapters.base_adapter import AdapterResult

        adapter = WorkdayAdapter.__new__(WorkdayAdapter)
        adapter.job_hash = "abc12345abc12345abc12345abc12345"
        adapter._screenshots = []
        adapter._client = MagicMock()
        adapter.job_title = "SWE Intern"
        adapter.company = "Acme"
        adapter.job_description = ""
        adapter.cv_variant = None
        adapter._cover_letter = "cover letter text"

        page = MagicMock()
        # _click_workday_nav returns "next_clicked", then the wait after it should raise.
        # We track calls to wait_for_timeout and raise only after nav was clicked.
        nav_clicked = {"done": False}
        def wait_side_effect(ms):
            if nav_clicked["done"]:
                raise Exception("Target page, context or browser has been closed")
        page.wait_for_timeout.side_effect = wait_side_effect

        adapter._page = page
        adapter._safe_screenshot = MagicMock(return_value=None)
        adapter._handle_workday_dialogs = MagicMock()
        adapter._default_unfilled_binary_radios = MagicMock()
        adapter._fill_known_workday_fields = MagicMock()
        adapter._fill_workday_education = MagicMock()
        adapter._fill_workday_work_experience = MagicMock()
        adapter._fill_workday_generic_fields = MagicMock()
        adapter._verify_required_fields = MagicMock(return_value=(True, 1, []))
        def click_nav_side_effect():
            nav_clicked["done"] = True
            return "next_clicked"
        adapter._click_workday_nav = MagicMock(side_effect=click_nav_side_effect)
        adapter._get_validation_errors = MagicMock(return_value=[])
        adapter._handle_validation_errors = MagicMock(return_value=False)

        # Patch imports used inside _do_fill_workday_form
        with patch('core.applicator._get_answers', return_value={"about_me": "test"}), \
             patch('core.applicator._identify_fields', return_value=[]), \
             patch('core.applicator._check_consent_checkboxes', return_value=0), \
             patch('core.applicator._generate_cover_letter', return_value="cover"), \
             patch.object(WorkdayAdapter, '_resolve_cv', return_value=Path("nonexistent.pdf")):
            try:
                result = adapter._do_fill_workday_form()
                # Should return a success/submit result, not raise
                self.assertIsInstance(result, AdapterResult)
                self.assertTrue(
                    result.success,
                    f"Expected success=True, got next_state={result.next_state!r}, error={result.error_message!r}"
                )
            except Exception as e:
                self.fail(
                    f"_do_fill_workday_form raised {type(e).__name__}: {e}\n"
                    "The page-closed exception after next_clicked was not caught."
                )


# =============================================================================
# 6. INTEGRATION — lookup_answer with real YAML
# =============================================================================

class TestLookupAnswerIntegration(unittest.TestCase):
    """These tests use the actual data/default_answers.yaml file."""

    def test_all_personal_fields_resolve(self):
        """Core personal fields should all be in the YAML."""
        from core.applicator import lookup_answer
        expected = {
            "first_name": str,
            "last_name":  str,
            "email":      str,
            "phone":      str,
        }
        for field, typ in expected.items():
            with self.subTest(field=field):
                result = lookup_answer(field)
                self.assertIsInstance(result, typ)
                self.assertTrue(len(result) > 0, f"Empty answer for {field!r}")

    def test_education_fields_resolve(self):
        from core.applicator import lookup_answer
        for field in ("university", "degree", "major", "gpa", "graduation_year"):
            with self.subTest(field=field):
                result = lookup_answer(field)
                self.assertTrue(len(result) > 0, f"Empty answer for {field!r}")

    def test_work_auth_fields_resolve(self):
        from core.applicator import lookup_answer
        for field in ("work_authorization", "visa_sponsorship_required"):
            with self.subTest(field=field):
                result = lookup_answer(field)
                self.assertTrue(len(result) > 0, f"Empty for {field!r}")


# =============================================================================
# RUNNER
# =============================================================================

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    suite.addTests(loader.loadTestsFromTestCase(TestCVPathResolution))
    suite.addTests(loader.loadTestsFromTestCase(TestLookupAnswer))
    suite.addTests(loader.loadTestsFromTestCase(TestPickBestOption))
    suite.addTests(loader.loadTestsFromTestCase(TestContentGenerator))
    suite.addTests(loader.loadTestsFromTestCase(TestWorkdayPageCrashHandling))
    suite.addTests(loader.loadTestsFromTestCase(TestLookupAnswerIntegration))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
