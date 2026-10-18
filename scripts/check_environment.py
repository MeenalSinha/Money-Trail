#!/usr/bin/env python3
"""Developer readiness check. Same behaviour as the money-trail-check command."""
import sys

from money_trail.cli import check_main

if __name__ == "__main__":
    sys.exit(check_main())
