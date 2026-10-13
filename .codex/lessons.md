# Lessons

- For distro-installed native test libraries, use the package manager file list rather than a recursive glob of `/usr/lib`. Recursive glob follows SDK symlinks and can consume the entire live-suite CI timeout before the application starts. Preserve the live assertions and test the bounded discovery path.
