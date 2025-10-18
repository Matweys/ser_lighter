"""
Multi-Account Coordinator System

Управляет 3 ботами (PRIMARY, SECONDARY, TERTIARY) для каждого символа.
Обеспечивает непрерывную торговлю через ротацию при застревании.
"""

from .multi_account_coordinator import MultiAccountCoordinator

__all__ = ['MultiAccountCoordinator']