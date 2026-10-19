import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPLOAD_LOADER_PATH = ROOT / "upload" / "prompts_loader.py"
SPEC = importlib.util.spec_from_file_location("upload_prompt_loader", UPLOAD_LOADER_PATH)
UPLOAD_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPLOAD_MODULE)


def test_upload_classification_prompts_render_growth_context_and_default_to_empty():
    for name in ("classify", "classify_single"):
        prompt = UPLOAD_MODULE.PromptLoader().render(name, growth_context="AXIS_CONTEXT_SENTINEL")
        assert "AXIS_CONTEXT_SENTINEL" in prompt
        assert "{growth_context}" not in prompt

        prompt_without_growth_context = UPLOAD_MODULE.PromptLoader().render(name)
        assert "{growth_context}" not in prompt_without_growth_context
