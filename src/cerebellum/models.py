"""Model identifier constants for OpenRouter."""

# Primary model
MODEL_PRIMARY = "openai/gpt-4o"

# Fallback models
MODEL_FALLBACK = "anthropic/claude-sonnet-4-6"

# Default model list for config
DEFAULT_MODELS = [MODEL_PRIMARY, MODEL_FALLBACK]

# Pricing (per 1M tokens)
_MODEL_PRICING_USD_PER_MTOK = {
    "openai/gpt-4o": {"input": 2.50, "output": 10.00},
    "anthropic/claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "anthropic/claude-opus-4-7": {"input": 15.00, "output": 75.00},
}
