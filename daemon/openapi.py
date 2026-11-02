"""Print the OpenAPI schema: uv run python -m daemon.openapi > docs/openapi.json"""

import json
import sys
from typing import Any
from unittest.mock import Mock

from daemon.api import create_app


def schema() -> dict[str, Any]:
    result: dict[str, Any] = create_app(Mock()).openapi()
    return result


if __name__ == "__main__":
    json.dump(schema(), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
