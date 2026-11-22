"""Shared identifiers for prospective advisor-rule evidence."""

import unicodedata


OPERATOR_ORIGIN = "registered_operator_ws_v1"


def valid_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 200
            and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in value))
