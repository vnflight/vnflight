import os
import sys


sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)


def test_generated_prompt_uses_canonical_act_command():
    from vnflight.lib import generate_prompt

    prompt = generate_prompt({"id": "demo", "name": "Demo Game"})
    # The prefix follows the layout (dist/ in a clone, flat in a release).
    from vnflight.lib import cli_command_hint
    prompt = prompt.replace(cli_command_hint(), "python vnflight.py")

    assert "python vnflight.py act <target>" in prompt
    assert "python vnflight.py act start --wait" in prompt
    assert "python vnflight.py act 1 --wait" in prompt
    assert 'python vnflight.py act "Continue" --wait' in prompt
    assert "python vnflight.py cmd start --wait" not in prompt
    assert "python vnflight.py choose" not in prompt
    assert "python vnflight.py click" not in prompt
