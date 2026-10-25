"""The verification recipes, keyed by name."""
from familysearch.api import OPERATIONS

from ..core import Recipe, evidence_problem
from ..reads import read_recipe_problem
from .pilot import PILOT

RECIPES: dict[str, Recipe] = {r.name: r for r in PILOT}


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
        evidence = evidence_problem(recipe.ops, recipe.evidence)
        if evidence:
            problems.append(f"{key}: {evidence}")
        if recipe.evidence == "read-content":
            problem = read_recipe_problem(recipe)
            if problem:
                problems.append(f"{key}: {problem}")
        if not callable(recipe.run):
            problems.append(f"{key}: run is not callable")
    return problems
