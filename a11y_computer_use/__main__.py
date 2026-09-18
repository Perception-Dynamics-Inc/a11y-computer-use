"""Allow ``python -m a11y_computer_use`` as an alias for the ``a11y_computer_use`` script."""

from a11y_computer_use.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
