"""Allow ``python -m adguard_cli_proxy``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
