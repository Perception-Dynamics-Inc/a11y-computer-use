"""Allow ``python -m computeruse`` as an alias for the ``computeruse`` script."""

from computeruse.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
