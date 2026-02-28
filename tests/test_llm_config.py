import os
from unittest.mock import patch
from lethe.memory.llm import LLMConfig

def test_detect_provider_gemini():
    """Test that Gemini provider is detected when GEMINI_API_KEY is present."""
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test_key", "LLM_PROVIDER": ""}):
        config = LLMConfig()
        assert config.provider == "gemini"
        # Verify default models (prefix added automatically)
        assert config.model == "gemini/gemini-3-flash-preview"
        assert config.model_aux == "gemini/gemini-3-flash-preview"
        # Verify temperature override for Gemini
        assert config.temperature == 1.0
        assert config.deterministic_temperature == 1.0

def test_detect_provider_gemini_explicit():
    """Test explicit Gemini provider setting."""
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test_key", "LLM_PROVIDER": "gemini"}):
        config = LLMConfig()
        assert config.provider == "gemini"
        assert config.model == "gemini/gemini-3-flash-preview"

def test_other_provider_temperature():
    """Test that other providers use standard temperatures."""
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test_key", "LLM_PROVIDER": "openrouter"}):
        config = LLMConfig()
        assert config.provider == "openrouter"
        assert config.temperature == 0.7
        assert config.deterministic_temperature == 0.3
