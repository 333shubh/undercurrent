"""The scheduled trigger's force/deliver flags (.github/scripts/derive-flags.sh).

Worth a test file of its own because this is the bug that cost two days of
digests. The flags used to be computed in a `${{ }}` expression:

    DELIVER="${{ inputs.deliver == false && '0' || '1' }}"

`inputs` is null on a `schedule`, and GitHub coerces null and false alike to 0
before comparing, so `null == false` held and every scheduled run asked the
service for `deliver=0`. The pipeline ran, spent its budget and posted nothing,
and the workflow still went green -- the one failure mode nobody notices.

So the assertion that matters is the boring one: with no inputs at all, the
scheduled run delivers.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".github",
    "scripts",
    "derive-flags.sh",
)

WORKFLOW = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".github",
    "workflows",
    "trigger-daily.yml",
)

BASH = shutil.which("bash")


def derive(**env) -> dict[str, str]:
    """Run the real script the workflow runs, and parse its name=value output."""
    proc = subprocess.run(
        [BASH, SCRIPT],
        capture_output=True,
        text=True,
        # Start from a clean slate so a stray FORCE_INPUT in the developer's
        # own environment cannot make these pass or fail by accident.
        env={"PATH": os.environ.get("PATH", ""), **env},
    )
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in proc.stdout.strip().splitlines())


@pytest.mark.skipif(BASH is None, reason="needs bash to run the workflow script")
class TestDerivedFlags:
    def test_scheduled_run_delivers(self):
        """The regression. A schedule passes no inputs; it must still post."""
        assert derive() == {"force": "0", "deliver": "1"}

    def test_empty_inputs_deliver(self):
        """Unset and empty are the same thing once GitHub interpolates them."""
        assert derive(FORCE_INPUT="", DELIVER_INPUT="") == {"force": "0", "deliver": "1"}

    def test_dispatch_defaults_deliver(self):
        assert derive(FORCE_INPUT="false", DELIVER_INPUT="true") == {
            "force": "0",
            "deliver": "1",
        }

    def test_dispatch_can_dry_run(self):
        """The feature the buggy expression was reaching for, still intact."""
        assert derive(DELIVER_INPUT="false")["deliver"] == "0"

    def test_dispatch_can_force(self):
        assert derive(FORCE_INPUT="true")["force"] == "1"

    @pytest.mark.parametrize("value", ["False", "FALSE", "0", "no", "null", "maybe"])
    def test_only_literal_false_suppresses_delivery(self, value):
        """Anything GitHub would not render for a false boolean input means
        deliver. A silent dry run is indistinguishable from success, so this
        side is the safe one to fail towards."""
        assert derive(DELIVER_INPUT=value)["deliver"] == "1"


class TestWorkflowWiring:
    """The script is only useful if the workflow actually reads its output."""

    def test_workflow_uses_the_script(self):
        body = open(WORKFLOW, encoding="utf-8").read()
        assert "derive-flags.sh" in body
        assert "steps.flags.outputs.deliver" in body
        assert "steps.flags.outputs.force" in body

    def test_workflow_does_not_compare_inputs_inline(self):
        """The exact shape of the old bug, kept out by name."""
        body = open(WORKFLOW, encoding="utf-8").read()
        assert "inputs.deliver ==" not in body
        assert "inputs.force &&" not in body
