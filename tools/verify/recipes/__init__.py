"""The verification recipes, keyed by name."""
from familysearch.api import OPERATIONS

from ..core import Recipe, evidence_problem
from ..reads import read_recipe_problem
from .pilot import PILOT
from .reads_genealogies import READS_GENEALOGIES
from .reads_misc import READS_MISC
from .reads_relationship_sources import READS_RELATIONSHIP_SOURCES
from .reads_standards import READS_STANDARDS
from .reads_tree_persons import READS_TREE_PERSONS
from .reads_tree_relations import READS_TREE_RELATIONS
from .writes_genealogies_persons import WRITES_GENEALOGIES_PERSONS
from .writes_genealogies_relationships import WRITES_GENEALOGIES_RELATIONSHIPS
from .writes_genealogies_sources import WRITES_GENEALOGIES_SOURCES
from .writes_genealogies_trees import WRITES_GENEALOGIES_TREES
from .writes_memories import WRITES_MEMORIES
from .writes_memory_links import WRITES_MEMORY_LINKS
from .writes_source_box import WRITES_SOURCE_BOX
from .writes_sources import WRITES_SOURCES
from .writes_tree_notes import WRITES_TREE_NOTES
from .writes_tree_persons import WRITES_TREE_PERSONS
from .writes_tree_relationship_details import WRITES_TREE_RELATIONSHIP_DETAILS
from .writes_tree_relationships import WRITES_TREE_RELATIONSHIPS

RECIPES: dict[str, Recipe] = {
    r.name: r for r in (*PILOT, *READS_TREE_PERSONS, *READS_TREE_RELATIONS,
                        *READS_RELATIONSHIP_SOURCES,
                        *READS_STANDARDS, *READS_MISC, *READS_GENEALOGIES, *WRITES_TREE_PERSONS,
                        *WRITES_TREE_NOTES, *WRITES_TREE_RELATIONSHIPS,
                        *WRITES_TREE_RELATIONSHIP_DETAILS, *WRITES_SOURCES,
                        *WRITES_SOURCE_BOX, *WRITES_MEMORIES,
                        *WRITES_MEMORY_LINKS, *WRITES_GENEALOGIES_TREES,
                        *WRITES_GENEALOGIES_PERSONS,
                        *WRITES_GENEALOGIES_SOURCES, *WRITES_GENEALOGIES_RELATIONSHIPS)}


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
