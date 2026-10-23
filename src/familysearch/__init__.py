"""Client library for the FamilySearch API."""

from .auth import Environment, detect_environment, extract_brave_token
from .client import FSClient
from .discovery import Discovery
from .errors import FSError, FSHTTPError, LinkNotFoundError, TokenExpiredError
from .memories import Memories
from .models import (
    FACT_BIRTH,
    FACT_DEATH,
    FACT_MARRIAGE,
    GENDER_URI,
    build_fact,
    build_name,
    build_person,
)
from .search import Search
from .sources import Sources
from .tree import TreeReader, TreeWriter

__all__ = [
    "FSClient", "Environment", "extract_brave_token", "detect_environment",
    "Discovery", "TreeReader", "TreeWriter", "Search", "Memories", "Sources",
    "build_person", "build_name", "build_fact",
    "FACT_BIRTH", "FACT_DEATH", "FACT_MARRIAGE", "GENDER_URI",
    "FSError", "FSHTTPError", "LinkNotFoundError", "TokenExpiredError",
]
