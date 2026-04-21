"""Blackbox browser automation service package."""

from blackbox_service.api import create_app
from blackbox_service.service import BlackboxService

__all__ = ["BlackboxService", "create_app"]
