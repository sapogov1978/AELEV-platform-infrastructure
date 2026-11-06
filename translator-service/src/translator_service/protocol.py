"""Shared validation primitives for Translator's public operator input."""

import re

LANGUAGE_PATTERN = r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$"
re.compile(LANGUAGE_PATTERN)
