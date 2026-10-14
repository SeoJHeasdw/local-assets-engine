"""Registered recipes. A recipe turns validated params into stages."""

from typing import Any

from .base import Recipe
from .image import IMAGE
from .mesh import IMAGE_TO_3D, TEXT_TO_3D
from .previz import PREVIZ
from .repair_mesh import REPAIR_MESH
from .refine_mesh import REFINE_MESH
from .edit_asset import EDIT_ASSET, IMPORT_IMAGE
from .video import IMAGE_TO_VIDEO, TEXT_TO_VIDEO
from .selftest import SELFTEST
from .schemas import params_schema

RECIPES: dict[str, Recipe] = {
    recipe.id: recipe for recipe in (IMAGE, IMAGE_TO_3D, TEXT_TO_3D, PREVIZ, REPAIR_MESH, REFINE_MESH, EDIT_ASSET,
                                     IMPORT_IMAGE, TEXT_TO_VIDEO, IMAGE_TO_VIDEO, SELFTEST)
}
DIAGNOSTIC_RECIPES = frozenset(recipe.id for recipe in RECIPES.values() if recipe.diagnostic)


def describe_recipes(recipes: dict[str, Recipe], presets: dict[str, Any]) -> list[dict[str, Any]]:
    """What ``GET /api/recipes`` lists: the work this engine accepts."""
    return [{"name": recipe.id, "label": recipe.label, "paramsSchema": params_schema(recipe.id, presets),
             "needsReview": recipe.needs_review} for recipe in recipes.values()]


__all__ = ["DIAGNOSTIC_RECIPES", "RECIPES", "Recipe", "describe_recipes"]
