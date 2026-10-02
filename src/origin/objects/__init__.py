"""Persistent object model for Origin."""

from origin.core.objects import CompactedError, ObjectNotFound, ObjectStore

__all__ = ["ObjectStore", "ObjectNotFound", "CompactedError"]
