# Reusable lessons

- Live tests that start Geany must use `--new-instance` when they own and clean up the subprocess. Geany can otherwise forward the document to an existing instance; terminating the launcher then leaves the editor and document alive, contaminating later tests. Keep pointer-click assertions intact when diagnosing the resulting failures.
