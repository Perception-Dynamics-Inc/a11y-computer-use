# outcome_gaps inert second click — #1500 (2026-11-06)

Head under test: `c0185f7c4e86161b9b4103654b24ebec1685f6ac`
Run: https://github.com/Perception-Dynamics-Inc/a11y-computer-use/actions/runs/42235569849

## What the logs show

Linux attempt 1, job 127952553261: `test_linux_chrome_outcome_gaps_for_app_click_tab_and_cover` FAILED at `tests/test_linux_live.py:11174`.

```text
AssertionError: ('unverifiable', 'unverifiable',
  'the accessibility state did not change; required action or context signals could not be read; re-observe first (snapshot/screenshot)')
```

`test_linux_chrome_type_ref_lands_only_in_the_named_field` PASSED on that attempt.

Linux attempt 2 (`--failed`), job 127974005329, same SHA: **outcome_gaps PASSED**. The job failed a different test, `test_linux_gtk_type_caret_unicode_and_disabled_controls`, with `ComputerUseError: the field text after type does not match what was inserted` at `a11y_computer_use/drivers/_atspi.py:10988`. That raise is inside `insert_text`, before `_judge_mutation`'s type settle. This PR does not change that path.

Package, Browser, Windows, macOS, and latency were green.

## Root cause of the outcome_gaps receipt

The second click on inert "Div button" has no snapshot between clicks. The effect snapshot did not change, so the verdict is `suspected_noop` / `the accessibility state did not change`. `outcome.retry_verdict` then requires a before and after `RetryEvidence`. A missing probe returns unverifiable and does not authorize a retry (`outcome.py`, the `probe is None` branch). That fail-closed rule is the #1231/#1234 family: a scoped AT-SPI retry read that cannot be completed is unknown, not a no-op.

`LinuxDriver.retry_evidence` / `_atspi._retry_evidence` returns `None` when the 40 ms read budget is spent, the native identity or focus cannot be read, or any exception escapes. Chrome AT-SPI does that intermittently on this inert button. The click path passes `requested=None`, so the #1500 missing-ref type settle does not run.

Turning that `None` probe into `suspected_noop` would authorize a retry the probe did not prove. No short product change does that honestly.

## What shipped

On the same #1500 branch, only this receipt is quarantined:

- `suspected_noop` still passes.
- The exact unread-probe unverifiable evidence does not fail the test. The cover, type, key, and background-tab assertions still run. If those pass, the test ends `pytest.xfail` with this reason.
- Any other second-click outcome still fails immediately.
- `retry_verdict` is unchanged. Type-ref settle and the Calc `len(reads) == 2` guard are unchanged.

The GTK caret mismatch on attempt 2 is a separate live flake. It is not quarantined here.
