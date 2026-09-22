"""Registered recipes. A recipe turns validated params into stages."""

from .base import Recipe
from .image import IMAGE
from .mesh import IMAGE_TO_3D, TEXT_TO_3D
from .previz import PREVIZ
from .repair_mesh import REPAIR_MESH
from .refine_mesh import REFINE_MESH
from .edit_asset import EDIT_ASSET, IMPORT_IMAGE
from .video import IMAGE_TO_VIDEO, TEXT_TO_VIDEO

RECIPES: dict[str, Recipe] = {
    recipe.id: recipe for recipe in (IMAGE, IMAGE_TO_3D, TEXT_TO_3D, PREVIZ, REPAIR_MESH, REFINE_MESH, EDIT_ASSET,
                                     IMPORT_IMAGE, TEXT_TO_VIDEO, IMAGE_TO_VIDEO)
}

__all__ = ["RECIPES", "Recipe"]
