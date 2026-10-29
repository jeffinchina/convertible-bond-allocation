import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
RUNNER = ROOT / "Run-EventAnalysis.ps1"
CONFIG = ROOT / "config.json"


class EventAnalysisRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = RUNNER.read_text(encoding="utf-8")
        cls.config = json.loads(CONFIG.read_text(encoding="utf-8"))

    def test_project_config_has_explicit_ordered_model_candidates(self):
        event_config = self.config["eventAnalysis"]
        self.assertGreaterEqual(len(event_config["modelCandidates"]), 2)
        self.assertEqual(len(event_config["modelCandidates"]), len(set(event_config["modelCandidates"])))
        self.assertIn(event_config["reasoningEffort"], {"low", "medium", "high", "xhigh", "max", "ultra"})

    def test_runner_prefers_desktop_global_cli_and_keeps_bundled_fallback(self):
        global_lookup = self.script.index("Get-Command codex.exe")
        bundled_check = self.script.index("Test-Path -LiteralPath $runtimeCli")
        self.assertLess(global_lookup, bundled_check)
        self.assertIn("$codexLauncher = 'global'", self.script)
        self.assertIn("$codexLauncher = 'bundled'", self.script)

    def test_runner_always_passes_model_and_reasoning_explicitly(self):
        self.assertIn("'--model', $model", self.script)
        self.assertIn('model_reasoning_effort=', self.script)
        self.assertNotIn("$prompt | & node $runtimeCli", self.script)

    def test_fallback_is_limited_to_model_compatibility_errors(self):
        for marker in (
            "model_not_found",
            "not supported when using Codex",
            "requires a newer version of Codex",
        ):
            self.assertIn(marker, self.script)
        self.assertIn("if (-not $modelCompatibilityFailure)", self.script)
        self.assertIn("throw \"Codex 事件分析失败", self.script)

    def test_stale_result_is_deleted_before_each_attempt(self):
        loop = self.script.index("foreach ($model in $modelCandidates)")
        delete = self.script.index("Remove-Item -LiteralPath $resultPath -Force", loop)
        invoke = self.script.index("& $codexCommand @codexArguments", loop)
        self.assertLess(delete, invoke)

    def test_model_retry_classifier_ignores_warnings_and_other_errors(self):
        # Load the function through the PowerShell AST without running the
        # event analysis script, so this exercises its actual retry decision.
        script_path = str(RUNNER.resolve()).replace("'", "''")
        command = f"""
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script_path}', [ref]$null, [ref]$null)
$function = $ast.Find({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-ModelCompatibilityFailure' }}, $true)
. ([scriptblock]::Create($function.Extent.Text))
$cases = @(
    'ERROR: {{"code":"model_not_found"}}',
    'ERROR: The model is not supported when using Codex with a ChatGPT account.',
    'ERROR: This model requires a newer version of Codex.',
    "warning: Model metadata for example not found.`nERROR: Network unavailable",
    'ERROR: You have hit your usage limit.',
    'ERROR: Authentication failed.',
    'ERROR: Invalid output schema.'
)
@($cases | ForEach-Object {{ Test-ModelCompatibilityFailure -OutputText $_ }}) | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            ["pwsh.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), [True, True, True, False, False, False, False])


if __name__ == "__main__":
    unittest.main()
