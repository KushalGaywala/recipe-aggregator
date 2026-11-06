"""Turns a dish name (plus optional notes) into a structured recipe using Claude."""

import os

import anthropic
from pydantic import BaseModel, Field

MODEL = os.environ.get("RECIPE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("RECIPE_EFFORT", "medium")  # low | medium | high | xhigh | max

# Models that accept the server-side refusal fallback on the Claude API.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """\
You write recipes for a home cook's personal recipe collection. Given the name of a dish \
and optional notes, write one dependable recipe for it that a home cook can follow.

Follow the cook's notes (diet, servings, equipment, ingredients on hand, spice level and so on). \
If the dish name could mean several things, pick the most common version and say which one in \
the summary.

Give every ingredient a quantity, in metric with common US measures in parentheses where they \
help. Write the steps in cooking order, with times, temperatures, and the signs that tell the \
cook something is done. Add tips only when they are useful: make-ahead, storage, substitutions \
or common mistakes."""


class Recipe(BaseModel):
    summary: str = Field(description="One or two sentences describing the dish.")
    servings: str = Field(description='How many it serves, e.g. "4".')
    prep_time: str = Field(description='e.g. "20 min".')
    cook_time: str = Field(description='e.g. "45 min".')
    ingredients: list[str] = Field(description='Each item with its quantity, e.g. "2 tbsp olive oil".')
    steps: list[str] = Field(description="Method steps in order, without numbering.")
    tips: list[str] = Field(description="Optional tips; may be empty.")


class GenerationError(Exception):
    """A problem worth showing to the user as-is."""


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def generate_recipe(name: str, description: str = "") -> dict:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise GenerationError("ANTHROPIC_API_KEY is not set, so new recipes can't be generated.")

    prompt = f"Dish: {name}"
    if description:
        prompt += f"\n\nNotes from the cook:\n{description}"

    extra = {}
    if MODEL in _FALLBACK_MODELS:
        # If a safety classifier declines, the API retries on a suitable model in the same call.
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

    try:
        response = _get_client().beta.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=Recipe,
            output_config={"effort": EFFORT},
            **extra,
        )
    except anthropic.AuthenticationError:
        raise GenerationError("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY.")
    except anthropic.RateLimitError:
        raise GenerationError("The Anthropic API is rate limiting requests. Try again in a minute.")
    except anthropic.APIStatusError as e:
        raise GenerationError(f"The Anthropic API returned an error ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        raise GenerationError("Couldn't reach the Anthropic API. Check the server's internet connection.")

    if response.stop_reason == "refusal":
        raise GenerationError("Claude declined to write this recipe. Try rewording the name or notes.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise GenerationError("The recipe came back incomplete. Please try again.")

    return response.parsed_output.model_dump()
