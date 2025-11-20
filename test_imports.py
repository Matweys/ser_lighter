#!/usr/bin/env python3
"""Тестовый скрипт для проверки импортов"""
import sys
import os

print(f"Python path: {sys.executable}")
print(f"Python version: {sys.version}")
print(f"Working directory: {os.getcwd()}")

try:
    import environs
    print("✅ environs imported successfully")
except ImportError as e:
    print(f"❌ environs import failed: {e}")
    sys.exit(1)

try:
    import pandas
    print("✅ pandas imported successfully")
except ImportError as e:
    print(f"❌ pandas import failed: {e}")
    sys.exit(1)

try:
    from core.settings_config import system_config
    print("✅ core.settings_config imported successfully")
except ImportError as e:
    print(f"❌ core.settings_config import failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("✅ All imports successful!")

