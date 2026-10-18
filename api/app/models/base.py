"""
RootOps V2 — Declarative Base

All ORM models inherit from this Base so Alembic can auto-detect them.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all RootOps models."""

