import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("build", ROOT / "tools" / "build.py")
build = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = build
SPEC.loader.exec_module(build)


class ParseAssignmentTests(unittest.TestCase):
    def test_parse_assignment_when_name_and_expression_are_valid_returns_the_pair(self):
        assignment = "hook_length=60"

        result = build.parse_assignment(assignment)

        self.assertEqual(result, ("hook_length", "60"))

    def test_parse_assignment_when_expression_contains_equals_preserves_the_expression(self):
        assignment = 'label_text_style="Flush" == "Flush" ? "Flush" : "Emboss"'

        result = build.parse_assignment(assignment)

        self.assertEqual(
            result, ("label_text_style", '"Flush" == "Flush" ? "Flush" : "Emboss"')
        )

    def test_parse_assignment_when_separator_is_missing_raises_a_build_error(self):
        assignment = "hook_length"

        with self.assertRaises(build.BuildError):
            build.parse_assignment(assignment)


class NormalizeModelTests(unittest.TestCase):
    def test_normalize_model_when_given_a_stem_returns_the_source_file(self):
        model = "coin_hex_bit"

        result = build.normalize_model(model)

        self.assertEqual(result.name, "coin_hex_bit.scad")

    def test_normalize_model_when_given_an_unknown_model_raises_a_build_error(self):
        model = "not_a_model"

        with self.assertRaises(build.BuildError):
            build.normalize_model(model)


class BuildEnvironmentTests(unittest.TestCase):
    def test_build_environment_when_managed_fonts_are_used_creates_a_project_local_fontconfig_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            original_tool_root = build.TOOL_ROOT
            build.TOOL_ROOT = temporary_root / ".tools"
            tools = build.ToolPaths(
                openscad=temporary_root / "openscad.com",
                bosl2=temporary_root / "BOSL2",
                fonts=temporary_root / "fonts & symbols",
            )

            try:
                environment = build.build_environment(tools)
            finally:
                build.TOOL_ROOT = original_tool_root

            config_path = Path(environment["FONTCONFIG_FILE"])
            self.assertEqual(config_path, temporary_root / ".tools" / "fontconfig" / "fonts.conf")
            self.assertEqual(environment["FONTCONFIG_PATH"], str(config_path.parent))
            self.assertIn("fonts &amp; symbols", config_path.read_text(encoding="utf-8"))


class PrintDiagnosticsTests(unittest.TestCase):
    def test_print_diagnostics_when_openscad_returns_output_prints_it_once(self):
        output = io.StringIO()

        with redirect_stdout(output):
            build.print_diagnostics("ECHO: render details\n")

        self.assertEqual(output.getvalue(), "ECHO: render details\n")

    def test_print_diagnostics_when_openscad_returns_no_output_prints_nothing(self):
        output = io.StringIO()

        with redirect_stdout(output):
            build.print_diagnostics("")

        self.assertEqual(output.getvalue(), "")
