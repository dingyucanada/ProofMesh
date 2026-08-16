import importlib.util
from pathlib import Path


def test_all_seven_reusable_skill_assets_pass_the_release_validator(capsys):
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts/validate_skills.py"
    spec = importlib.util.spec_from_file_location("validate_skills", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    assert "Skill assets valid: 7/7" in capsys.readouterr().out
