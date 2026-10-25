"""The verification recipes, keyed by name. Empty until recipes are written."""
from familysearch.api import OPERATIONS

from ..core import EVIDENCE, Recipe

RECIPES: dict[str, Recipe] = {}


def validate_recipes(recipes: dict[str, Recipe]) -> list[str]:
    """Problems with a recipe set: unknown ops, key/name mismatch, bad evidence."""
    problems = []
    seen: dict[str, str] = {}
    for key, recipe in recipes.items():
        if not isinstance(recipe, Recipe):
            problems.append(f"{key}: not a Recipe")
            continue
        if key != recipe.name:
            problems.append(f"{key}: key does not match recipe name {recipe.name!r}")
        if recipe.name in seen:
            problems.append(f"{key}: duplicate recipe name {recipe.name!r} (also {seen[recipe.name]})")
        seen[recipe.name] = key
        if not recipe.ops:
            problems.append(f"{key}: recipe has no ops")
        for op in recipe.ops:
            if op not in OPERATIONS:
                problems.append(f"{key}: unknown operation {op}")
        if recipe.evidence not in EVIDENCE:
            problems.append(f"{key}: evidence must be one of {EVIDENCE}, got {recipe.evidence!r}")
        if not callable(recipe.run):
            problems.append(f"{key}: run is not callable")
    return problems
