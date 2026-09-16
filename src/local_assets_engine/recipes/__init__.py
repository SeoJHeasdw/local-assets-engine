"""Registered recipes. A recipe turns validated params into stages."""

from .base import Recipe
from .image import IMAGE
from .mesh import IMAGE_TO_3D, TEXT_TO_3D
from .previz import PREVIZ
from .repair_mesh import REPAIR_MESH
from .refine_mesh import REFINE_MESH

RECIPES: dict[str, Recipe] = {
    recipe.id: recipe for recipe in (IMAGE, IMAGE_TO_3D, TEXT_TO_3D, PREVIZ, REPAIR_MESH, REFINE_MESH)
}

__all__ = ["RECIPES", "Recipe"]
