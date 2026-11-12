"""The review screen: a FastAPI backend (web/app.py) and a TypeScript page (web/frontend, built into web/static)."""
from .app import create_app

__all__ = ["create_app"]
