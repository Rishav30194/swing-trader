"""
Test configuration.

TRADING_CAPITAL is deliberately required in production — the app must refuse to
start rather than guess how much money to deploy. Tests set a known value here,
before src.config is imported, so the suite does not depend on the developer's
.env. python-dotenv does not overwrite variables already present in the
environment, so this takes precedence.
"""

import os

os.environ.setdefault("TRADING_CAPITAL", "1000")
