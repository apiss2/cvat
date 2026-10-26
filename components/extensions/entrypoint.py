#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import sys

import manage
from components.extensions.catalog import register

if __name__ == "__main__":
    try:
        register(manage)
        manage.main()
    except (manage.OperationError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
