# Changelog

All notable changes to a11y-computer-use are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases from 0.1.1 on are
published on PyPI as `a11y-computer-use`.

Each line describes one non-merge commit and ends with its short hash and author date (the date `git log --date=short` prints). Branch-integration merge commits carry no changes of their own and are not listed.
Within a group, lines are ordered by theme, then by date.

## [Unreleased]

### Added

- `click`, `type`, `key`, `set_value`, `select`, `scroll`, `menu`, `app`, and `window` keep the sentence they already returned. The same result carries `outcome` (`confirmed`, `suspected_noop`, `unverifiable`, `partial`, or `refused`), `next` (an ordered list of `ref`, `coordinates`, `cdp`, `keyboard`, and `foreground`), and a short `evidence` string. MCP tools put those fields in structured content. The text content is still the sentence. The agent follows `next` when it recovers, and it counts `suspected_noop` and `unverifiable` as one screen when a clock moves the snapshot digest. The outcome comes from a read-back, a change in the accessibility state, or the target process still being alive. A click that exits the target is `partial`, not `confirmed`. A ref whose node is still alive in a hidden document, including a background Firefox tab, is `not_showing` with `outcome` `refused`. A node that is gone stays `stale_ref`. Hermetic tests cover each tool. Live GTK checks cover a Save click (`confirmed`), a second press of an inert button (`suspected_noop`), and a button that quits the process (`partial`). A live Firefox test refuses the old Name ref after that tab is in the background (#118) (ede0a70, 2026-10-09).

### Fixed

- An exited process that the parent has not reaped counts as dead on macOS and Windows, the same as a Linux zombie. On macOS, `kill(pid, 0)` still succeeds for a zombie, so `pid_alive` reads the `ps` state and treats `Z` as dead. The hermetic test kills a child, polls for up to 2 seconds, and asserts `pid_alive` is false before `wait`. On Windows, `os.kill(pid, 0)` is `CTRL_C_EVENT`. The Windows job on `89e5074` stopped in `subprocess.py` with `KeyboardInterrupt` after 6 passed tests. `pid_alive` opens the process and treats any exit code other than `STILL_ACTIVE` as dead (dc9f229, 2026-10-09).
- The live Firefox background-tab test selects the Form Probe tab again before it returns. Firefox exposes that tab as a button, so a role check for `tab` never clicked it and the contenteditable test still saw the Privacy Notice page (be575e3, 2026-10-09).

## [0.4.53] - 2026-10-09

### Fixed

- Typing non-ASCII text into a Qt line edit inserts that text and nothing after it. On 0.4.45, `type` of `ünï` into a field that held `Start` produced `Startünï` plus a NUL and an extra character, and `type` of `日本` produced `Start日本` plus a NUL and more, and each call still returned `typed N characters`. GTK's insert length is a UTF-8 byte count. Qt's `InsertText` does `QString::resize(length)` and that length is a character count, so the byte count extends the string with uninitialized characters. `GetText` is a D-Bus string and stops at the NUL, so the text compared equal. Qt now gets the character count. The caret position was already a character offset. A Qt read-back also requires the character count to equal the string that was asked for, on `type` and on `set_value`. A byte-length fake, and a GTK field, still pass the UTF-8 byte count. A live Qt line edit, started from `Start`, accepts `abc`, `x y`, `ünï`, and `日本`, and `!` typed after `ünï` lands as `Startünï!`. The widget's own Python text matches the snapshot (#129) (2026-10-09).
- A Qt snapshot no longer shows an uninitialized Value on labels, empty text, checks, radios, and rows. On 0.4.45 those read as `6.9e-310` or `0.0` because the Value interface was preferred whenever the text was empty. Text, or the accessible name, is what a non-range role shows. Value is read for a slider, a spin button, or a progress bar, and on Qt only when the minimum and maximum are a real range (finite, minimum below maximum, neither end a subnormal). A real zero on a 0–100 slider is still shown. A GTK label that exposes Value `0.0`, and a GTK slider, are unchanged. A Qt combo box is titled from its label relation. On Linux the accessible name is the current item (`Red`), so the label (`Qt color`) was lost and `set_value` `Blue` returned `text_mismatch` and stayed on Red. The combo has no Selection interface, and `Toggle` on a list item does not change the current item. `set_value` opens the popup and clicks the item's center, then reads the accessible name back. An option that is not in the list is still `invalid_arguments` before any click. A live Qt form shows the label, the empty line edit, the check, the radio, and the list row with no numeric value, the slider at 40, the progress bar at 25, and the spin box at 3; the combo is `Qt color` with value `Red`, and `set_value` `Blue` leaves the widget on Blue with the popup closed (#131) (2026-10-09).

## [0.4.52] - 2026-10-09

### Added

- UI text that reaches a model can be wrapped in nonce-tagged `<untrusted>` boundaries. Phrases that read like instructions are marked and kept. Agent observations are fenced by default. MCP tool output stays unfenced unless `A11Y_COMPUTER_USE_FENCE_UNTRUSTED` or the server option is set. Browser navigation and actions to a disallowed origin fail with `domain_blocked`. Allow and block lists are `Agent(allowed_domains=..., blocked_domains=...)`, the `a11y-agent` flags, and the MCP server option. A live Linux Chrome page under AT-SPI contains an injection string and a link to a blocked domain; a scripted model does not follow it. A live headless Chromium check reads the document URL from CDP `Page.getFrameTree` and a link href from the DOM (#121) (8573ea9, 2026-10-09).

## [0.4.51] - 2026-10-09

### Fixed

- On Linux, `type` and `key` with `app=` resolve that app's window from the EWMH list and the AT-SPI application, focus it when it is not already active, and then send the input. A window that cannot be focused is `focus_changed`. An app with no window is `app_not_found`. The error does not say the call is macOS-only. A live pair of GTK windows, with the other window in front, receives `landed-type` from `type` and `z` from `key` in the named entry only. Hermetic tests use a fake driver and fake X objects (#140) (2026-10-09).
- An empty or whitespace `app` is `invalid_arguments` on every tool that accepts `app=`. It is not treated as omitted and does not ask for a permission grant of a blank name (#144) (2026-10-09).

## [0.4.48] - 2026-10-09

### Added

- Live tests for the M1 computer-use agent. A scripted model (not an LLM) drives `Agent.run` through a GTK note window and local Chrome pages: it saves a file and checks the bytes, fills a form, stops for a human on login, a one-time code, a card number, and a captcha iframe, recovers when a wrong `done` fails its evidence check, and changes strategy after the screen is stuck. `a11y-agent run --json` exits 0, 1, 2, or 3. The loop is [docs/agent.md](./docs/agent.md) (2026-10-09).

## [0.4.47] - 2026-10-09

### Fixed

- `set_value` and `type` enter text into a Firefox web text field, number field, and textarea, and `set_value` changes a `<select>`. On 0.4.45, against Firefox ESR on a local page (Name, Count, Notes, Color), `set_value` "Ann Lee", "12", "line1", and "Blue" each returned `unsupported` with reason `text_mismatch` and the fields stayed empty (Color stayed Red). `type` "Bo" after a click on Name returned `the field text after type does not match what was inserted` with `actual: ""` and `inserted_chars: 2`. The same miss happened on Count and Notes. `key` into a focused Notes field did land. EditableText on these fields returns true and leaves the DOM empty. When that read-back is unchanged, the field is focused and the text is sent as key events, and success is a later read of that same field. A read that changed to something else is still `text_mismatch`, with no key fallback. A `<select>` tries the option's action first. When that does not change the selected option, focus plus Up or Down moves by the option index. The popup is not opened: Escape on a collapsed Firefox menu undoes a selection that already landed, because the menu stays VISIBLE while collapsed. The selected option is read back. A live Firefox ESR window on that local page sets Ann Lee, 12, line1, and Blue, then types Bo, 9, and Hi after the fields are cleared. The page log records the input event (#127) (2026-10-09).
- A Firefox snapshot and `find` no longer include a background tab or the preloaded New Tab page, and a click on a link in a hidden tab is an error. On 0.4.45 a window with a local page in front and "Firefox Privacy Notice" behind it, plus the preloaded New Tab page, put those documents in the tree with on-screen bounds. `find` for Wikipedia returned the New Tab shortcut, and a click on Products in the hidden tab returned `clicked` while the screen did not change. A scroll pane that is not SHOWING and wraps an internal frame or a document web is dropped, and so is an internal frame that is not SHOWING. A document web is kept only when its frame hierarchy is SHOWING and its title is the selected page tab. The tab strip button for the background page stays, because that control is on screen. A click or press on a handle that still points at a hidden document returns `unsupported` with reason `not_showing` and does not run the link action. A live Firefox window shows Name and the "Firefox Privacy Notice" tab, does not show Products or Wikipedia, and a raw AT-SPI handle for the hidden Products link raises `not_showing` from both press and click (#128) (2026-10-09).
- Controls inside a paragraph or an empty label stay in the snapshot and in find. On 0.4.45 a paragraph whose text was only U+FFFC (object replacement) was an empty static-text leaf, and the walk dropped it before the button, checkbox, or text field inside. The same drop hid a checkbox, radio, or text field inside a `<label>` with no text of its own, including the Cloudflare Turnstile checkbox "Verify you are human", which AT-SPI exposes as a child of `label ''`. An empty label or paragraph is still not listed on its own when it has no surviving descendants. When it has one control, that control replaces it. When it has several, they stay parented under it. A childless empty static text and an unlabelled image are still dropped. A live Chrome page and a live Firefox page list Bravo, Para button, Bare para input, Para checkbox, the docs link, Div button, Name, Accept terms, Option one, Your answer, Icon checkbox, Bare checkbox, and Verify you are human, in both the full and the interactive snapshot. find matches those names. The Verify you are human checkbox is clicked and reads checked (#124, #136) (2026-10-09).
- A paragraph's value includes the text of its inline children. On 0.4.45 Chromium's U+FFFC characters were deleted, so `The <a>quick brown</a> fox <b>jumps</b> over the <em>lazy</em> dog.` read `The  fox jumps over the  dog.` and find could not match "quick brown fox" or "The quick". Each U+FFFC is replaced from AT-SPI Hypertext with that child's text, or with its name when the child has no text and the name is not already in the surrounding words. A choice control is not expanded, so a select still shows the selected option and a label around a select still reads "Country". find matches the sentence because the value contains it, and a NBSP in that text matches a space in the query. The same sentence is the paragraph value on the CDP accessibility tree. A live Chrome page and a live Firefox page both find "quick brown fox" and "The quick" (#130) (2026-10-09).
- `set_value` on a Chrome contenteditable no longer empties the editor when the write cannot be verified. On 0.4.45 `set_value` "Set 0" cleared the editor to a `<br>` (the text read back as a newline), then returned `unsupported` with reason `text_mismatch`, and "Hello world" was gone. A newline, a space, or a NBSP counts as a cleared field, so the new text is typed. If the read-back still does not match, the original text is typed back and the result is `text_mismatch`. A live Chrome page sets Editor A to Set 0, Set 1, and Set 2, and the snapshot value is that string. On the CDP backend the editor's text is replaced and checked against `innerText` with NBSP read as a space and one trailing newline ignored. A mismatch restores the previous `innerHTML` and returns `text_mismatch` (#125) (2026-10-09).
- `type` into a contenteditable no longer reports a false `text_mismatch` for a NBSP or a U+FFFC in the read-back. On 0.4.45 typing "  two spaces end " into Editor A returned `unsupported` while the editor showed the words, because the read-back used U+00A0. Typing "ZZ" into Editor B returned `unsupported` with actual `"\ufffc\ufffc"` while the paragraph contained ZZ, and a retry typed it twice. NBSP compares as a space. U+FFFC is expanded to the child text before the comparison, on the keystroke read-back and on an EditableText insert whose parent string stays an object character. A live Chrome page types the spaced suffix and ZZ, and both land. A live headless Chrome page types into the editor and the DOM contains the characters (#126) (2026-10-09).
- Firefox contenteditable `type` no longer reports `unchanged` after the key fallback has landed the text. On 0.4.47 the #127 key fallback typed "  two spaces end ", "ZZ", and " more" into the editor, then returned `text_mismatch` with `"unchanged": true` and the pre-key string (`Hello world`, `\ufffc\ufffc`, or `\n`), so a retry typed it again. `unchanged` compares the expanded text, NBSP as a space, not the raw object-replacement string. The key-fallback read waits briefly and uses that same text. `set_value` on a Firefox contenteditable no longer leaves the editor empty when the write cannot be verified: the original text is typed back. A live Firefox ESR page sets Editor A to Set 0, types the spaced suffix and ZZ, and types " more" into an emptied editor, and each call reports success (#151) (2026-10-09).

## [0.4.45] - 2026-10-08

### Fixed

- A ref click on a Chrome list or list-box row selects that row, and a miss does not clear the selection that was already there. On 0.4.44 a plain ref click on a multi-select listbox (`Apple`, `Banana`, `Cherry`, `Date`, with Banana and Date selected) selected the row 4 of 6, then 3 of 6, 1 of 12, and 2 of 4 times. A click on Date while Banana was selected could return `selection_unchanged` and leave the list empty. The option's `select` action toggles, and `Selection.select_child` issued while that action is still landing cancels it. A Chromium list-box or list row is selected by a click at the row's center, which is what a coordinate click does, and the option action is not fired first. A plain click replaces the selection, the same as a click in the page. If that click does not leave the row selected, the options that were selected before the click are selected again and the result is `unsupported` with reason `selection_unchanged`, not `clicked`. A GTK tree or list still uses the parent's Selection interface. A live Chrome page clicks Apple, Banana, Cherry, and Date, and Red, Green, Blue, and Gray on a single-select list, and each row is selected (#105) (2026-10-08).
- `app launch` of an app that is already running reports the window the running instance opened. On 0.4.44 `app launch mousepad` while Mousepad was open returned `unsupported` with reason `process_exited` and exit code 0, four times out of four, while the running instance opened Untitled 2, 3, 4, and 5. The launcher process exits 0 and the existing window id does not change; the title does. Exit 0 with a window of that app already up keeps waiting for a new window or a retitle, and the result names that title, including `activate: false`. Exit 0 with no window of the app (`true`) is still `process_exited` on the first look, and a non-zero exit (`false`, `xmessage` with no arguments) is still immediate. `gtk-launch` exiting 0 still waits, and a non-zero launcher exit still fails with that code. A program that stays up and never shows a window is still `timeout` with reason `no_window`. `app focus` of an absolute path resolves the same way launch does. On 0.4.44 `app focus /usr/bin/mousepad` returned `app_not_found` while `app list` showed `mousepad`. The path's basename matches the process comm or the WM_CLASS, and focus activates that window (#109) (2026-10-08).
- `set_value` on an empty Chrome number input that has a minimum and a maximum fills the field, and it does not report success while the field is empty. On 0.4.44 an empty Guests input (`min=0`, `max=12`) answered `set_value` 7 and `set_value` 3 with `unsupported`, reason `text_mismatch`, and the value read back as 0.0 while the DOM stayed `""`. `set_value` "0" reported success and the field stayed empty, because the Value interface reads the minimum when the text is empty. `Value.set_current_value` does not change that empty input. The digits are typed into the field, and success is the text. An empty text does not match 0. A number input that already holds a value still uses the Value interface. `set_value` "" still clears it. A GTK spin button is unchanged. A live Chrome page clears a min/max number input and refills it with 0, 3, and 7, and a status element updated by the input event shows those digits (#113) (2026-10-08).
- `key alt+s` while another top-level menu is open switches to the menu with that mnemonic. On 0.4.44 Alt+S while Edit was open left the menu state at `["Edit"]` until Escape, because the chord was delivered into the open menu. The letter is pressed as that top-level menu. A letter that names the menu already open is still delivered into it. The `menu` and `key` tool descriptions say that `key` does not close an open menu, and that alt+letter switches to the matching top-level menu (2026-10-08).

## [0.4.44] - 2026-10-08

### Fixed

- A Chrome `<select>` accepts a real option again. On 0.4.43 `set_value` "Peru", "Japan", and the already-selected "Kazakhstan" failed 6 of 6 times on a fresh page load with `unsupported: the value read back does not match 'Peru'` and reason `text_mismatch`. The value stayed Kazakhstan, the dropdown was left expanded, and the next call then failed with `popup_open`. 0.4.42 had set all three. Chromium reads the selected option's text the same way a snapshot does, including an option nested under the select, so the already-selected value is recognized and the popup is not opened for it. The combobox itself has no Selection interface: its name stays the aria-label and its text stays U+FFFC. `Selection.select_child` on the child menu returns false and leaves the HTML value alone. The option's `select` action is what changes it. A click or press action, and a click at the option's center after the popup is opened, are the fallbacks when that action does not land. The read-back polls the selected option's text for a short bounded interval, because the SELECTED state can trail the action, and that text is the same value the snapshot shows. Any popup this call opened is closed, including when the value does not land. An option that is not in the list is still `invalid_arguments` and the message lists the options, before any selection. The GTK combo path is unchanged: a non-editable combo still uses the combo's own Selection model index, an editable combo is still written on its own entry, and a highlighted popup row is still not a successful read-back. A GTK spin button whose step is 1 no longer accepts 4.6 as the held value. On 0.4.43 the adjustment stored 4.6 while the field showed 5, and the tool reported 4.6. The number is rounded to the control's step (or to 1 when the step is missing and the field shows an integer), the rounded value is what is written, and the result reports that value (`set eN = '5'`). A number outside the minimum and maximum is still rejected before the write. A Chrome number input is not a spin adjustment: 4.6 stays 4.6, and `set_value` "" clears the field. On 0.4.43 an empty string was rejected, so a number that held 3 could not be cleared. The same empty string clears an `<input type=number>` on the CDP browser backend. A GTK spin button still rejects "" (#103) (2026-10-08).
- A ref click on a Chrome multi-select listbox row selects the row. On 0.4.43 a plain ref click on the Cherry row returned `clicked e55 (AXRow 'Cherry')` with no selection and no error, 5 of 6 times. 0.4.42 selected 6 of 6. The row's Selection parent is found through a scroll pane or filler. A click action is used only when the row is still selected after a short beat. Otherwise the list's Selection selects that row, and if that does not stick a click is sent at the row's on-screen center. The selection is read back on every one of those paths. A click that does not select is `unsupported` with reason `selection_unchanged`, not `clicked`. The GTK tree fix is unchanged: a cell whose first action is expand or edit is still selected through the parent's Selection, not by firing that action. A snapshot of a multi-select listbox shows every selected option. On 0.4.43 a listbox with Banana and Date selected showed only Banana. The value is the selected options joined with ", " (`Banana, Date`). A single selected option is still that one name, and a GTK combo still shows its active item rather than a highlighted row in the popup (#105) (2026-10-08).
- `key` delivers to an open menu instead of closing it. On 0.4.43 the snapshot header said key presses go to the open menu, but `key down` in Mousepad's Edit menu returned `pressed down (closed open menu Edit first)`, the menu state became closed, and `key return` inserted a newline into the document (`line one` then a blank line then `line two`). `key alt+e` opened Edit and the following `key down` closed it and moved the caret. `key` no longer sends Escape first. Arrow keys, Return, and alt+letter reach the open menu, and Return does not reach the document. The focus gate still refuses a different named app that is in front. An override-redirect popup that leaves the frontmost name empty or `unknown` is still the gated app's menu, and that menu is the key target. `type` and `click` still close an open menu first. A context menu, which `menu(action=state)` does not report, is unchanged (#108) (2026-10-08).
- `app launch` reports a window only when one appeared, and it reports the window this launch opened. On 0.4.43 `app launch true` (exit 0) and `app launch xmessage` with no arguments (exit 1, and a zombie under the server) each waited about 61 seconds and returned `launched NAME; no window appeared within 60s` with no error. Both now fail on the first look with `unsupported`, reason `process_exited`, and the exit code in the message (`exited with status 0`, `exited with status 1`). The child is reaped, so it is not left defunct. An absolute path such as `/usr/bin/mousepad` opened a window in about a second and the call still waited 60 seconds and said no window appeared; `app focus` of that path then returned `app_not_found` while `app list` showed `mousepad`. The window is matched by the new process id (or a descendant), or, for a window that was not already open, by the binary's basename, WM_CLASS, or the desktop file's exec and StartupWMClass. The result names that window's title. A window that was already open is not reported as the one this launch created, so a second Mousepad document is `Untitled 2` rather than the existing `*Untitled 1`. Success requires that matching window. A Linux launch that stays running and never shows one is `timeout` with reason `no_window`, not a success string. `gtk-launch`, `xdg-open`, and `gio` still exit 0 before the real window maps; that exit is not treated as the app exiting, and a non-zero exit from one of them still fails immediately with that code. macOS and Windows, which have no process handle, still return `launched NAME; no window appeared within Ns` when no window appears. A name that is not on PATH is still `app_not_found` and does not wait (#109) (2026-10-08).
- `app quit` reports an unsaved-changes dialog on Linux. On 0.4.43 Mousepad with unsaved changes answered `sent quit to mousepad; it is still running`. The prompt is a top-level AT-SPI `alert` named "Question" (Cancel, Don't Save, Save), and the snapshot showed it as `e1 window "Question"` because the roles `dialog` and `alert` were mapped to `AXWindow`. Both roles are now `AXDialog`. A window that is `_NET_WM_WINDOW_TYPE_DIALOG`, or transient for a parent, is the same dialog even when the snapshot role is still a window. Quit returns `sent quit to mousepad; it is showing a dialog (likely unsaved changes) and needs a human decision`. Nothing is clicked: Discard and Don't Save are not pressed. Quit still only sends the driver's quit chord (`ctrl+q` on Linux) (#110) (2026-10-08).
- Keystroke `type` and `key` no longer follow Caps Lock or Num Lock. On 0.4.43, with Caps Lock on, `type` of `echo Hello MiXed` into a terminal or any field without EditableText produced `ECHO hELLO mIxED` and still reported `typed 16 characters`. `key b` produced B and `key shift+c` produced c. The AT-SPI EditableText insert path was already correct. The XTEST path now chooses Shift so the requested letter is what the server emits while Caps Lock stays where the user left it: Caps Lock XOR Shift, and no Caps Lock key is pressed. Digits and punctuation are not inverted. A keypad digit is selected by Num Lock, not by Shift. When the wanted keysym needs the other Num Lock state, Num Lock is toggled for that tap and restored afterwards, and Shift is not held. Where the focused node's text can be read, the typed string has to show up there. A terminal screen that shows the inverted string is `unsupported` with reason `text_mismatch`, not `typed N characters`. When the text cannot be read, the count of characters sent is still the result (#111) (2026-10-08).

## [0.4.43] - 2026-10-08

### Fixed

- `set_value` reports success only when the value read back matches. On 0.4.42 a GTK combo box (items Red, Green, Blue) accepted `set_value` "Green" as `set eN = 'Green'` while the combo stayed on Red, its popup stayed open, and the text was typed into a different focused entry (Paris became Green). With nothing else focused, "Blue" still left the combo on Red and the popup open. An editable combo's own ref appended ("Blue" became "BlueLima") and still reported success. A non-editable combo is set through the combo's own Selection (the model index), not by highlighting a row in its popup menu. An editable combo is written with set_text_contents on its own entry and sends no keystrokes, so a different focused field is left untouched. The snapshot value of an editable combo is that entry's text. An option that is not in the list is `invalid_arguments` and the message lists the options, before any selection or keystroke. Any popup this call opened is closed. A Chrome `<select>` given "Mars" (options Kazakhstan, Japan, Peru) and a number input given "abc" used to report success while the select stayed on Kazakhstan and the number became empty. Both are `invalid_arguments` before any input, and the select message lists the options. Every path reads the value back; a mismatch is `unsupported` with reason `text_mismatch`, not `ok`. The 0.4.41 refusal to type a value into a menu, heading, label, or button is unchanged (#103) (2026-10-08).
- GTK spin buttons and sliders take `set_value` through the AT-SPI Value interface, so the application's value changes and not only the text. On 0.4.42 a spin button (0 to 10, value 3) reported "7" and "15" as set while `gtk_spin_button_get_value` stayed 3; "15" later clamped to 10 once focus left the field. A number outside the minimum and maximum is `invalid_arguments` and the message includes both, before the value is written, so 15 and -2 leave the spin at 3. "abc" is `invalid_arguments` as well. A slider, and any other control that exposes Value and is not a text field or a scroll bar, accepts `set_value` the same way. On 0.4.42 a slider returned `unsupported` ... `is not editable`. The current value is read back and must match (#104) (2026-10-08).
- A ref click on a GTK tree or list row selects the row. On 0.4.42 `click` on a row or cell inside a Selection container returned `clicked` and left the selection where it was, 5 of 5 times, because the cell's first action is expand or edit and that action reports success. The row is selected through the parent's Selection interface, or by a click at the row's on-screen center when that interface does not move the selection. The selection is read back. If it did not move onto the row, the result is `unsupported` with reason `selection_unchanged`, not `clicked`. A coordinate click and a double click are unchanged. A Chrome listbox option still selects on a ref click (#105) (2026-10-08).
- Chrome snapshots show form-control state. On 0.4.42 a `<select>` and a listbox showed U+FFFC (the object replacement character) as their value, one per option, and a label that contained the select read `Country ￼`. The value is the selected option's text. An `aria-pressed` toggle never showed as pressed after the AT-SPI state gained `pressed`; it now shows `checked`, including `unchecked` when the attribute is false, the same flag a GTK toggle and a `role=switch` already use. An empty number field read `="0.0"` because the text was empty and the Value interface's default was used; it now reads as empty. A number that actually holds 3 still shows 3, and a slider still shows its Value. Check boxes, radio buttons, and tabs are unchanged. The same three fixes apply to the AT-SPI Chrome path and to the CDP browser backend (#106) (2026-10-08).

## [0.4.42] - 2026-10-08

### Fixed

- Linux `type` into a GTK editable inserts the whole string at the caret. On 0.4.41 the AT-SPI `insert_text` length was the character count, and the GI binding wants the UTF-8 byte count, so `Привет` landed as `При`, `中文字` as `中`, and `ok 😀` inserted nothing, while the tool still said `typed N characters`. A binding that takes a character count still gets that count. The text is inserted at the caret. A selection is deleted first and the text replaces it, so a caret at the start of `world` plus `hello ` is `hello world`, and a selected `DROP` in `keep DROP keep` becomes `keep NEW keep`. A CRLF is one newline. The reported count is the number of characters the field read back. When that read does not match, the result is `unsupported` with reason `text_mismatch` (or `selection_not_replaced` when the selection is still there) and it is not a success. The keystroke path, which is what Chrome uses when the field has no EditableText, still types the text and now collapses a CRLF to one newline as well. A GTK single-line entry (AT-SPI role `text` with `SINGLE_LINE`) is `AXTextField`. A multi-line view stays `AXTextArea`. A coordinate click does not remember a ref, so `type` looks up the focused editable of the frontmost app and uses that same insert helper. `Привет`, `中文字`, `ok 😀`, and `ab ✓ ok` are inserted whole. A focused node with no EditableText still uses keystrokes (#100) (2026-10-08).
- `click`, `hover`, `scroll`, `drag`, `set_value`, and an `act` step on a ref the tree marks disabled return `element_disabled` and send no press, pointer input, or value write. This applies on every backend that reports the enabled state (not sensitive, or not enabled). A raw point has no such flag. On Linux, `menu press` of a disabled item still returns `unsupported` with reason `disabled`, and the menu that was opened to reach the item is closed with Escape. Pressing the menu-bar entry again does not leave GTK menu tracking, which is why Edit stayed open after a disabled Undo. If Escape leaves the menu open, the error reason is `menu_still_open` (#101) (2026-10-08).
- `window list` with `app=""` is `invalid_arguments`. An unfiltered list when nothing is focused returns the windows whose apps already have a read grant, and when none do it returns `unsupported` with reason `no_focused_window`. It does not ask for a grant of `unknown`. On Linux, `window list` bounds and `window move` both use the client window inside the frame. Move subtracts `_NET_FRAME_EXTENTS` before the NorthWest `_NET_MOVERESIZE_WINDOW`, so a move to (100, 80) lists the client at (100, 80) (#97) (2026-10-08).

## [0.4.41] - 2026-10-08

### Fixed

- `window list app=X` matches the app id exactly, ignoring case. On 0.4.40 the filter treated the row's app name as a substring of X, so a window with no `_NET_WM_PID` and an empty app name was included in every filtered list: `app=mousepad` also returned an xmessage window, and `app=thunar` for a granted app that was not running returned that same window. An empty app name never matches a filter now, and a name that is only a substring of another app (`mouse` against `mousepad`) does not either. A macOS owner name still matches the bundle id whose last component is that name (`TextEdit` and `com.apple.TextEdit`), and a Linux launcher alias still matches its comm (`google-chrome` and `chrome`). A window with no pid uses its WM_CLASS instance when that property is set (`xmessage`), so the row has an app id and `raise` can gate on it. When the owner still cannot be identified, `raise` and the other mutating verbs return `unsupported` with reason `owner_unknown`. The message says the owning app could not be identified. It does not say `needs_permission` and it does not tell the user to grant an empty name. A minimized window (ICCCM iconic or `_NET_WM_STATE_HIDDEN`) is `on_screen: false` with `bounds: null`. `focus`, `minimize`, `maximize`, `move`, `resize`, and `close` are real verbs in the schema, the tool description, and the Runtime. On Linux X11 they send EWMH or ICCCM client messages (`_NET_ACTIVE_WINDOW`, `WM_CHANGE_STATE` plus `_NET_WM_STATE_HIDDEN`, both maximized atoms, `_NET_MOVERESIZE_WINDOW`, `_NET_CLOSE_WINDOW`), gated at click against the owning app, the same as `raise`. `move` requires x and y; `resize` requires a positive width and height. A backend that cannot perform a verb returns `unsupported` and names the platform. It does not return `invalid_arguments` or `not a valid WindowVerb`. An unknown action string still names the verbs that exist. Each window probe and each verb opens its own X connection and closes it before the call returns, including when the window is missing, so a live poll does not fill the display server's client table and a later flush is not a connection the server already dropped (#97) (2026-10-08).
- Linux clipboard `read` returns the text target's bytes decoded as UTF-8. On 0.4.40 the reader used `text=True`, so `a\r\nb` came back as `a\nb` and a lone CR was rewritten the same way. CR, CRLF, NUL, and the other bytes that already survived are unchanged. A text target that exists and holds zero bytes still returns `""`. The other empty results are errors, not `""`: the clipboard holds only non-text data such as `image/png` (`clipboard_not_text`); the text is not valid UTF-8 (`clipboard_invalid_utf8`, and the bytes are not replaced); the clipboard has no owner or the text target is not available (`clipboard_no_owner`); no `xclip`, `xsel`, or `wl-paste` is on PATH (`missing_clipboard_tool`). `write` with none of those tools installed returns that same unsupported error and names `xclip`, `xsel`, and `wl-clipboard`. It is not `internal_error` and it does not say to report a defect. A lone surrogate in a write, including `run-once`, is one line, `invalid_arguments: clipboard: ...`, exit 2, with no traceback. Unicode, NUL, and a 4,000,000-character write are still passed through as UTF-8 bytes. The write passes those bytes on stdin and sends the tool's stdout and stderr to `/dev/null`. xclip keeps the selection by forking; capturing its output left that child holding the pipe, so the write timed out and never owned the selection. The child still owns it after the call returns, and a later read returns the same bytes. `run-once` reports every other `ValueError` as `invalid_arguments` as well (#98) (2026-10-08).

## [0.4.40] - 2026-10-08

### Fixed

- An app left out of a non-empty allow list is refused as not on the allow list, and `grant` reports the grants a list blocks. On 0.4.39, Mousepad granted `full` with `"allow": ["xfce4-terminal"]` was refused `deny: mousepad is on the deny list; no actions are permitted`, and the audit row stored that same reason, though the deny list was empty. The refusal and the audit row now say the app is not on the allow list. An app that is on the deny list still says so, and that reason wins when both lists would block it. `a11y-computer-use grant` with no app printed `no apps granted` when grants existed but a deny or allow list blocked them, and printed the same line for a `permissions.json` that was not valid JSON, then exited 0. It now lists every grant and marks a blocked one `blocked: on the deny list` or `blocked: not on the allow list`. A broken file prints one line, `permission configuration is invalid or unreadable; repair <path>`, and exits 1. `grant <app> <tier>` and `grant --revoke <app>` on that file used to exit 1 with a traceback ending in `ValueError: permission configuration is invalid or unreadable; repair <path> before changing grants`. They print that one line and exit 1, and the file is left as it was. `grant '' read` used to fall through to the list. An empty app name is rejected. `grant --revoke` of an app that was never granted printed `revoked <app>` and exited 0; it now says the app wasn't granted and does not print `revoked`. A broken configuration still fails closed: no grant is revived from it (#95) (2026-10-08).

## [0.4.39] - 2026-10-08

### Fixed

- `wait_until` `url_status` keeps one deadline for the whole probe. On 0.4.38 plain HTTP stayed inside `timeout_s`, and a handshake that never sent bytes stopped at 3.02 seconds for `timeout_s` 3. `https://httpbin.org/delay/8` with `timeout_s` 5 still took 6.0–6.6 seconds, and with `timeout_s` 2 it took 3.19 seconds, with `last_error` `TimeoutError: The read operation timed out`. A local server that sent response headers one byte every 0.5 seconds ran 29.5 seconds at both `timeout_s` 3 and `timeout_s` 5 and then returned success with a 200 match. Each socket operation was given a fresh timeout of the time remaining, so the TLS handshake and the following read could each spend it, and a trickle that arrived inside the per-read timeout was never cut off. The GET now runs on one worker and is abandoned at the deadline: the sockets it opened are closed, and a status that shows up after that is a timeout, not a success. DNS stays on its own bounded lookup. A resolved loopback, private, link-local, multicast, or reserved address is still refused before anything connects. `A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS=1` still opts out of that refusal. A timeout detail still includes `last_status` or `last_error`, and the file observations from 0.4.37. On Windows the 404 case in the mission suite lost `last_status`: the worker dropped a status when `join` returned with the clock already past the deadline, and the next poll then replaced that status with a socket error from closing the probe. A status read before the probe is abandoned is kept, and a later poll that does not see a new status does not erase it. A 200 that arrives only after the probe is abandoned is still a timeout. `file_exists` on a directory, or on a glob that only matches a directory, still fails immediately (#91) (2026-10-08).
- `click`, `hover`, `scroll`, `drag`, and the same `act` steps reject a point outside the target display before any input. On 0.4.38 an off-screen point such as (5000, 5000), (-10, -10), or the display's own (width, height) was clamped to the nearest edge and reported as the point that was asked for. A coordinate past ±32767 crashed in `Xlib.ext.xtest.fake_input` with `struct.error` (`'h' format requires -32768 <= number <= 32767`): MCP `internal_error`, and `run-once` an uncaught traceback with exit 1. An unknown `display_id` was treated as display 0, and `click` still said it had used the id that was asked for. `zoom` of a region entirely off-screen returned a black image and success, and a huge width was accepted. Valid pixels are 0..width-1 and 0..height-1 in that display's own coordinates. A monitor whose global origin is negative still uses that range, so local (0, 0) on it is valid; the driver adds the origin when it posts the event. An unknown `display_id` is `invalid_arguments` and the message lists the valid ids. `zoom` of a region that misses the display is `invalid_arguments`. A region that crosses the edge is clipped, and the result text names the clipped rectangle. The `act` check is in the same pre-pass as the other argument checks, so a later bad coordinate does not run earlier steps. `run-once` prints `invalid action:` and exits 2, with no traceback. `file_exists` and `file_stable` still reject a directory on the first look, and a timeout detail still includes the last observation (#93) (2026-10-08).

## [0.4.38] - 2026-10-08

### Fixed

- `wait_until` `url_status` returns within `timeout_s`. On 0.4.37 each probe used a fixed 10 second request timeout, and `socket.getaddrinfo` ignores that timeout, so a probe that started near the deadline kept running. `http://nonexistent.invalid/` with `timeout_s` 5 returned after 10.0 seconds, and a slow endpoint returned after 7.1 seconds. Each probe is now limited to the lesser of 10 seconds and the time still left. That budget covers DNS, the connect, and the read. DNS runs on a daemon thread and the wait joins it for at most the budget, because the HTTP client cannot interrupt a lookup. The connection uses the addresses that lookup returned, so it does not resolve the name again. No probe starts once the deadline has passed. `file_exists`, `file_stable`, and `settle` have no request timeout of their own; their only wait is the poll sleep, which was already clipped to the time left, and they now also skip a poll after the deadline. A timeout detail still includes `last_status` or `last_error` (a DNS timeout, a connection error, or a read timeout) and the file observations from 0.4.37. A resolved loopback, private, link-local, multicast, or reserved address is still refused before anything connects. `A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS=1` still opts out of that refusal (#91) (2026-10-08).

## [0.4.37] - 2026-10-08

### Fixed

- `wait_until` `file_exists` on a directory, or on a glob that only matches a directory, fails immediately instead of waiting out the timeout. On 0.4.36 the match went through `is_file()`, so `{"file_exists": "~/.a11y-computer-use"}` and a glob such as `~/some-existing-folder/su*` polled until `timeout_s` (600 by default) and then reported only `timeout: condition not met`. The documented contract is a regular file (`min_bytes` defaults to 1). A path that already exists and is not a regular file, or a glob whose matches are all non-files, raises `ValueError` on the first look, which the MCP layer reports as `invalid_arguments: wait_until: ... exists but is not a regular file; file_exists only matches regular files`. A path that is not there yet still waits, so a download can appear. `file_stable` uses the same rule, and `min_bytes` still applies only to regular files. A timeout detail still has `condition`, `waited_s`, and `polls`, and now adds the last observation: `last_status` or `last_error` (connection error class and message) for `url_status`; `exists`, `path`, `last_size`, and `min_bytes` for the file conditions (`stable_for_s` while a `file_stable` candidate is watched); `found` for `snapshot_text` and `screen_text`; `elapsed_s` and `settle_s` for `settle`. Paths outside the home directory and non-public URL addresses are still refused before any wait (#89) (2026-10-08).

## [0.4.36] - 2026-10-08

### Fixed

- An `act` key step folds a `modifiers` list into the chord, and a failed batch is an MCP tool error. On 0.4.35, `{"do":"key","chord":"a","modifiers":["ctrl"]}` and `"modifiers":"ctrl"` both returned `pressed a`. The modifier was dropped, a plain `a` was typed, and the audit row was an ordinary key chord. A list is now part of the chord, modifiers first, the same string the standalone `key` tool presses: `["ctrl"]` and `a` press `ctrl+a`, and `["ctrl","shift"]` press `ctrl+shift+a`. A modifier already in the chord is not repeated. A string modifier and an unknown modifier are `invalid_arguments`, the same check a click step uses (`'modifiers' must be a list of modifier names`, `unknown modifiers`). Any other field a step does not accept, on every step type, is `invalid_arguments` naming that field, and no step runs. A printable chord built this way still refuses a focused password field, and the audit stores `chord: "[REDACTED]"` with `chars: 1`. `ctrl+a` built the same way is still sent. When validation fails or any step fails (`secure_field` included), the MCP `act` result has `isError: true` and the body is still the per-step JSON. On 0.4.35 that result had `isError: false`, so a client that only checked the flag treated the batch as a success (#87) (2026-10-08).

## [0.4.35] - 2026-10-08

### Fixed

- An `act` step with a missing or wrong-typed field returns `invalid_arguments` and runs nothing. On 0.4.34, `{"do":"key"}`, `{"do":"type"}`, `{"do":"wait_for"}`, and `{"do":"key","keys":"b"}` failed with the bare KeyError text `'chord'`, `'text'`, or `'ref'`. A non-string `chord`, a non-list `modifiers`, a non-number scroll delta, a non-list drag `path`, and a non-number `timeout_s` failed the same way, with `TypeError` text, and only after earlier steps in the batch had already run. Every step is checked before the first action. `click`, `hover`, and `scroll` still require a ref or both coordinates (`target an element ref, or both x and y coordinates`). `drag` names `start_ref` or `start_x`/`start_y`, and `end_ref` or `end_x`/`end_y`. `type`, `key`, and `wait_for` name the missing `text`, `chord`, or `ref`. A wrong type names that field (`'chord' must be a string`, `'modifiers' must be a list of modifier names`, `timeout_s must be finite and nonnegative`). The step error is `invalid_arguments: {step type}: step {index}: ...`, the same prefix a standalone tool uses (`invalid_arguments: key: empty chord ''`). A later invalid step does not leave earlier steps done. A failure while a step is running (stale ref, secure field, unsupported hover, the batch time budget) still stops the batch and keeps the earlier results (#83) (2026-10-08).

## [0.4.34] - 2026-10-08

### Fixed

- Linux Chrome snapshot keeps the Figma login form when the zero-height wrapper has a click action. On 0.4.33, live Google Chrome on `https://www.figma.com/login` still printed only `group "Login | Figma"`. The raw tree is `document web` "Login | Figma" 1271 by 708, then `section#react-page` 1271 by 0 with AT-SPI actions `click` and `showContextMenu`, then a section 1271 by 708 with `clickAncestor` and `showContextMenu`, then a section, a form, entry "EMAIL", password text "PASSWORD", and the buttons. `click` maps to AXPress, so the 0.4.33 hollow-wrapper rule treated the section as interactive and dropped it with the form. `clickAncestor` and `showContextMenu` are not press actions. An unnamed, valueless, non-focusable zero-size wrapper under a Chromium document is kept when a descendant with a real box survives, and its rect is the union of those descendants. It is not a clickable target, and a ref click does not call `AtspiAction.do_action` on it. A focusable, named, or genuinely interactive zero-size node (push button, link, check box, entry) still drops. macOS and Windows nodes do not carry the web mark. On this VM (Xvfb 1280x800, dbus-run-session, at-spi-bus-launcher, Google Chrome 148.0.7778.96 with `--force-renderer-accessibility`, `--no-sandbox`), `snapshot --app chrome` of `https://www.figma.com/login` listed textfield "EMAIL", securetextfield "PASSWORD" (secure, value none), button "Continue with Google", and button "Log in". The synthetic tree uses the same action names (#79) (#56) (2026-10-08).
- A printable `key` chord refuses a focused password field, and the audit log does not store the chord. On 0.4.33, `type` returned `secure_field` for a focused password field, but `key` with `{"chord": "a"}` returned `pressed a`, a masked character landed in the field, and the audit row was `keychord {"chord":"a"}` in the clear. A printable chord is one character, or shift plus that character, with no ctrl, alt, super, cmd, or fn. `key` and an `act` key step use the same focused-password probe as `type` on Linux (AT-SPI STATE_FOCUSED on a password text node), macOS (secure event input or AXFocusedUIElement), Windows (UIA IsPassword), and the browser (document.activeElement). Tab, Shift+Tab, Enter, Escape, arrows, Backspace, Delete, Home, End, and modifier shortcuts are still sent. Every printable chord is stored as `chord: "[REDACTED]"` with `chars: 1`, on every platform, whether or not the probe caught the field. Other chords stay in the row unless the action was already secure. No fill tool is added (#81) (2026-10-08).

## [0.4.33] - 2026-10-07

### Fixed

- Linux Chrome snapshot keeps a page that sits under a zero-height section. On 0.4.32, live Google Chrome with `--force-renderer-accessibility` on `https://www.figma.com/login` (Xvfb, dbus-run-session, at-spi-bus-launcher) exposed `document web` "Login | Figma" 1279 by 812, then a `section` 1279 by 0, then a `section` 1279 by 812, then a `section`, a `form`, entry "EMAIL", password text "PASSWORD", push buttons "Continue with Google" and "Log in", and links. `snapshot --app chrome` printed only `group "Login | Figma"` with no children. `_prune_inner` dropped any node whose bounds were zero-size, and the zero-height section took the painted form with it. Chrome does expose those children. Under a `document web`, a `document frame`, or an `internal frame`, a zero-width or zero-height non-interactive wrapper is kept when a descendant with a real box survives, and its rect is the union of those descendants, the same rule as GTK's negative extent. A zero-size node outside that web content, a zero-size control, and a positive rect that lies off every display still drop. macOS and Windows nodes do not carry the mark, so their snapshots and token budgets are unchanged. The snapshot read keeps the zero size; hit-testing and scrolling still treat it as no box. `file_dialog` stays unsupported on Linux. Synthetic AT-SPI tree, not a live Chrome run (#56) (2026-10-07).
- Linux Chrome snapshot reaches a named control inside a nested cross-origin iframe. On 0.4.32, `https://2captcha.com/demo/recaptcha-v2` exposed `internal frame` "reCAPTCHA" → `document web` "reCAPTCHA" → several `section`s → check box "I'm not a robot" (focusable, showing, 29 by 29 at about 367, 401). The snapshot showed the reCAPTCHA groups and then `… 2 more`. `MAX_DEPTH` (12 kept levels) cut the checkbox. AT-SPI role `internal frame` maps to `AXGroup`. A nested `internal frame`, or a `document web` already inside a page, restarts the kept-depth budget. A web subtree already past the cap still opens such a frame, bounded by four restarts per snapshot (one per iframe; the document directly under that frame does not spend a second restart), the raw-depth backstop of 64, and 64 extra reads. Ordinary deep nodes stay elided. A ref click on the checkbox uses the existing `AtspiAction.do_action` path. hCaptcha and reCAPTCHA image-challenge tiles have no accessible names and stay unsolvable without vision. FunCAPTCHA / Arkose canvas is opaque. No OCR is added. Synthetic tree, not a live Chrome run (#76) (2026-10-07).
- The audit log never stores typed text. `type`, `set_value`, an `act` type step, and typing addressed to an app all record a `TypeText`. On 0.4.32 the characters were written in the clear unless the action had already been flagged secure. A `type` aimed at a password or one-time-code field that could not be focused was not flagged, so the secret landed in the JSONL file. Every `TypeText` now stores `text` as `[REDACTED]` and `chars` as the character count, on every platform, whether or not a secure field was detected. Key chords are still redacted only when the action is secure. Clipboard-write text stays redacted unconditionally. Secure fields stay refused for `set_value`, `click`, and `type`. No fill tool is added (#54) (2026-10-07).

## [0.4.32] - 2026-10-07

### Fixed

- Linux snapshot and `scroll_to_find` list the on-screen rows of a long Chromium list past the first 250 children. On 0.4.31, live Google Chrome 148.0.7778.96 on DISPLAY=:1 (XFCE, X screen 1920x1200, Chrome window about 1282 by 802, not the reported Xvfb 1280x800) showed a 520px `overflow-y:scroll` list of 2000 rows at 18px. With ITEM-0201 through ITEM-0229 painted, `_in_view_named_rows` returned ITEM-0201 through ITEM-0224 (24 names) while the AT-SPI bounds of those children were inside the list box through ITEM-0229. With ITEM-0226 through ITEM-0254 painted, the snapshot listed ITEM-0226 through ITEM-0237. After that the snapshot listed no rows, and `scroll` returned `unsupported` with `reason=rows_stale` while the list kept moving. The walk counted every visited node from child 0, including rows above the viewport, against `_VISIBLE_WALK_CAP` (250). An unnamed `list item` plus its `static` text is two nodes, and the direct-child scan was `range(min(count, 250))`. Once the first visible child index was at or past 250 the walk never reached those rows. The on-screen walk now starts at the first child whose box reaches the viewport when the child count is above that cap. A run that is not top-to-bottom is still read from child 0. Rows the accessibility tree does not expose are not added. After the change, the same live page listed 29 snapshot rows matching the paint: ITEM-0001 through ITEM-0029, ITEM-0201 through ITEM-0229, ITEM-0226 through ITEM-0254, and ITEM-0456 through ITEM-0484. `scroll` with dy=5 and unit=lines returned no error and moved five rows (ITEM-0001 to ITEM-0006, ITEM-0201 to ITEM-0206, ITEM-0226 to ITEM-0231, ITEM-0456 to ITEM-0461), each call about 0.84 to 0.86 seconds. `scroll_to_find` found ITEM-0240 after 43 scrolls in 42.3 seconds (paint ITEM-0216 through ITEM-0244), ITEM-0270 after 49 scrolls in 50.0 seconds (paint ITEM-0246 through ITEM-0274), and ITEM-0400 after 75 scrolls in 75.8 seconds with max_scrolls=80 (paint ITEM-0376 through ITEM-0404). With max_scrolls=60, ITEM-0400 returned `not found after 60 scroll(s)` in 59.6 seconds; the snapshot and the paint were ITEM-0301 through ITEM-0329, and the result was not `rows_stale`. Sixty steps of five rows from ITEM-0001 do not put ITEM-0400 on screen. Synthetic trees of the same 2000-row shape returned the same scroll counts (43, 49, and 75) and the same 60-scroll not-found window (#70) (2026-10-07).

## [0.4.31] - 2026-10-05

### Fixed

- Linux `scroll_to_find` on a list inside an overflow:auto wrapper anchors on the titled document instead of a larger empty Chrome panel. The published 0.4.29 wheel and the published 0.4.30 wheel both missed the same page: a 420px `overflow-y:auto` div around a plain 200-row list, with the address query only `?n=`. Before each search the tree showed ITEM-001 through ITEM-015. `scroll_to_find` for ITEM-040, ITEM-100, and ITEM-040 again each returned not found after 25 scrolls in about 6 seconds, and the tree then showed ITEM-186 through ITEM-200. 0.4.30 did not change this scroll path. A judged five-row step pauses to read the head, so that timing is an unjudged wheel. The content-height list is taller than the window, so the anchor is a page group under 90% of the window. The empty panel between the document and the window was larger than the titled document and still under that cutoff. The list walk from that panel reads 30 children and stops before the document, so no overflow list was found and each step sent a wheel. The wheel moved the wrapper in large jumps and still returned success, so the search never turned around. On 0.4.28, ITEM-100 was found after 6 scrolls. ITEM-040 already ended on ITEM-186 through ITEM-200. The titled document is the anchor when any ancestor group has a title. From that document the overflow list is found and `scroll_to` steps five rows, which overlaps ITEM-040 and ITEM-100. An untitled tree still uses the largest group. A fixed-height list smaller than the window stays the list anchor. dy=5 on that list still moves five rows. A body-scroll page still wheels the document. `unit=pixels` still writes the AT-SPI scroll bar and does not send a wheel. `file_dialog` and `set_value` are unchanged. Synthetic trees are not that live page. A local Chrome on DISPLAY=:1, not Tester's display :58 or :59, found ITEM-040 after 5 scrolls and ITEM-100 after 17, and the head moved five rows each step (#63) (2026-10-05).

## [0.4.30] - 2026-10-05

### Fixed

- Linux `file_dialog` returns the documented unsupported error when the named app is not frontmost. On 0.4.29 a call with `app` set to a background app, and a call naming a granted app that is not the frontmost app, returned `focus_changed` and told the caller to re-observe and retry. The grant is still checked. The Linux driver then raises `unsupported: file_dialog is not supported on Linux: GTK and portal file choosers are not driven by this tool`. The call does not open a chooser. Type, key, click, and `file_dialog` on the other drivers still use the frontmost recheck. Synthetic frontmost, not a live desktop (#68).
- Linux `set_value` on an element that is not editable raises an error that names the element and says it is not editable. On 0.4.29 a Mousepad `menu "File"`, a Chrome heading, a label, and a button returned `set eN = '<value>'`. The menu path opened the menu and typed the value into the document. The refusal sends no keystrokes, clicks, or focus changes. An editable field still succeeds when `Text.get_text(0, -1)` equals the new string: GTK `set_text_contents` replaces, and a Chromium field whose write appends is cleared and written again. A field whose text cannot be read at all is still trusted when `set_text_contents` returned true. Synthetic AT-SPI trees, not a live Mousepad or Chrome run (#69).

### Changed

- `CHANGELOG.md` records the released 0.4.27 snapshot fix under `[0.4.27]`. `docs/linux-port.md` states that the MCP server registers 25 tools on Linux, macOS, and Windows, and that the browser driver adds `console`, `network`, and `webmcp`. Chromium `unit=lines` is described as a one-row scroll-bar step, then `scroll_to`, then a track click, with the XTEST wheel only when those do not move the list. A content-height list and a document group still use the wheel. The count is `len` of `EXPECTED_TOOLS` in `tests/test_server.py` plus the three browser-only registrations in `build_server` (#71).

## [0.4.29] - 2026-10-05

### Fixed

- Linux line scroll keeps a five-row step when the grab stays at or under the uniform-row floor. On the 0.4.28 retest, dy=5 and unit=lines on a fixed-height list returned success with heads ITEM-001, ITEM-032, ITEM-037, ITEM-068, ITEM-073, ITEM-078, ITEM-109 (moves of +31, +5, +31, +5, +5, +31). The +31 frame painted ITEM-031 at the top of the list while the tree's first row was ITEM-032, so ITEM-030 was in neither window. On overflow:auto the heads were ITEM-001, ITEM-019, ITEM-024, ITEM-042, ITEM-047, ITEM-065, ITEM-070, ITEM-088 (moves of +18 and +5). `scroll_to` had already moved five rows. The grab stayed under 0.4, the driver treated that as no move, and the track click then paged. A head that moved by about the requested lines is that step, including under the floor. A page-sized jump is not, and a far hit-test name on a still grab is not. No track click or wheel follows the five-row step. A content-height list and a document group keep the wheel. `unit=pixels` still writes the AT-SPI scroll bar and does not send a wheel. Synthetic trees, not Tester's display :55 (#61) (2026-10-05).
- Linux `scroll_to_find` matches ITEM-040 in an overflow:auto list instead of scrolling past it. On the 0.4.28 retest the same five-line step plus a page skipped the window that contains ITEM-040 (ITEM-024 through ITEM-038, then ITEM-042). Both searches returned not found after 25 scrolls with the wrapper at ITEM-186 through ITEM-200. ITEM-100 was on screen after 6 scrolls. The five-row step now stands on its own, so the search lists ITEM-040 while it is on screen and does not finish on ITEM-186. A content-height list and a document group keep the wheel. `unit=pixels` still writes the AT-SPI scroll bar and does not send a wheel. Synthetic trees, not Tester's display :55 (#63) (2026-10-05).

## [0.4.28] - 2026-10-04

### Fixed

- Linux line scroll moves one content row per line. On the 0.4.26 retest a fixed-height list with dy=5 and unit=lines stepped 15 rows (ITEM-001, ITEM-016, ITEM-031). An overflow-y:auto list stepped 14 rows (ITEM-001, ITEM-015, ITEM-029). Five lines now moves five rows, and stays within the rows already on screen so the next frame still overlaps. A content-height list and a document group keep the wheel. `unit=pixels` still writes the AT-SPI scroll bar by the requested pixels and does not send a wheel. Synthetic trees, not a live Chrome run (#61) (2026-10-04).
- Linux `scroll_to_find` matches a row inside an overflow:auto wrapper as that row comes on screen. On the 0.4.26 retest ITEM-040, ITEM-100, and ITEM-040 again each returned not found after 25 scrolls, with the wrapper painted at ITEM-186 through ITEM-200. Chromium leaves the list item unnamed and puts the row text on a child; a default marker named "•" was taken as the head, so a real move did not count and a wheel followed. A marker is not a row. The head is the row text inside the wrapper. The same one-row line step reveals it through `scroll_to`, and the search matches ITEM-040 and ITEM-100. A fixed-height overflow list, a content-height list, and a document group keep their existing path. `unit=pixels` still writes the AT-SPI scroll bar and does not send a wheel. Synthetic trees, not a live Chrome run (#63) (2026-10-04).

## [0.4.27] - 2026-10-04

### Fixed

- Linux Chromium snapshot and find list every on-screen row of a list. On 0.4.22 through 0.4.26 a 520px overflow list with 18px rows painted about 29 rows, and a body-scroll page painted rows past the 16th, but the snapshot stopped at 16 (ITEM-001 through ITEM-016, or ITEM-172 through ITEM-187 at the bottom) and showed no elision marker. `find` and `scroll_to_find` could not match a painted row past that window, including ITEM-020 and, at the bottom, ITEM-190, ITEM-199, and ITEM-200. `scroll_to_find` for those last rows then ran to a still page, turned around, and failed with `page_unchanged` (dy=-1) after the list had been scrolled back to ITEM-001. The 16-row cap is the hit-test sample count, and the on-screen walk was using it as the row set. The walk now keeps every on-screen row. When that is more than the snapshot child cap, the rows are grouped so the cap does not drop the tail. The hit-test sample count stays 16. `scroll_to_find` ITEM-180 on a fixed-height list still succeeds. A still page that has not shown the target still turns around, and a second still page is still `page_unchanged`. `unit=pixels` still writes the AT-SPI scroll bar and does not send a wheel. Synthetic trees, not a live Chrome run (#65) (2026-10-04).

## [0.4.26] - 2026-10-04

### Fixed

- Linux line scroll keeps a uniform-row move as the one step. On the 0.4.25 retest a fixed-height list with an explicit ref, dy=5, and unit=lines jumped about 15 rows or 30–50, so rows in between were never shown, and every call still returned success. On overflow-y:auto, 3 or 4 of 6 calls returned `page_unchanged` at `mean_abs` about 0.5 to 0.7 while the list had moved 15–29 rows. The pixel check treated a grab at or under 1 as no move before it read the head, undid the bar write, and sent a track click or a wheel. A grab above the uniform-row floor (0.4) whose on-screen head leaves the old row is that step: the bar write stays, and no track click or wheel follows. A still grab at or under that floor is not a step. `page_unchanged` is only when the head stays and the grab stays at or under 1. The step is still about three rows per line, and not more than the rows already on screen. A content-height list and a document group keep the wheel. `unit=pixels` still writes the AT-SPI scroll bar by the requested pixels and does not send a wheel. Synthetic trees, not a live Chrome run (#61) (2026-10-04).
- Linux tools aimed at an app that is not running return `app_not_found`. On 0.4.24 and 0.4.23 a granted app with no process and no window got success-style replies: app focus said it had activated the app, `desktop_snapshot` returned an empty window plus a custom-drawn hint, find said no elements match, menu state said closed, menu close said no menu was open, and `scroll_to_find` said not found after 0 scrolls. Menu list already returned `app_not_found`. Snapshot, find, `scroll_to_find`, menu state, menu close, and app focus now do too. App focus does not say it activated an app that was never launched. A running app whose tree is empty stays an empty snapshot. A missing AT-SPI bus stays the closed menu and the permission error, not `app_not_found`. Synthetic fakes, not a live desktop (#62) (2026-10-04).
- Linux `scroll_to_find` matches rows that an ancestor overflow container scrolls. On 0.4.22 and 0.4.23 a plain list inside an overflow:auto wrapper did not scroll itself. `scroll_to_find` ITEM-040 reported not found after the wrapper had painted ITEM-186 through ITEM-200, so the search had gone past the row. The snapshot head sat about two rows above the painted head; those rows were hidden by the wrapper. The same targets were found when the list itself was the scroll container. The painted viewport is now the nearest ancestor that is shorter than the list and is not the document or the frame. Rows that ancestor hides are not the head. The same bar, `scroll_to`, and track checks scroll that wrapper, and no wheel is sent when the step moves the head. A fixed-height overflow list whose parent is the document, a content-height list whose parent is the document, and a document group keep their existing path. Synthetic trees, not a live Chrome run (#63) (2026-10-04).

## [0.4.25] - 2026-10-04

### Fixed

- Linux line scroll clicks the vertical track of a fixed-height overflow list when writing that list's AT-SPI scroll bar does not move the painted rows. On the 0.4.24 retest the list was an AXList 1239 by 422 at (20, 139). The bar step and AT-SPI `scroll_to` left the paint on ITEM-001, and `scroll_to_find` ITEM-180 ended on `page_unchanged` (dy=-1, `mean_abs` 0). A left click on the lower track moves the painted rows one page down. The upper track moves them up. The list pixels and the on-screen head still have to change. No wheel is sent when the click moves the list. The bar write is still tried first. A content-height list and a document group keep the wheel. `unit=pixels` still writes the AT-SPI scroll bar by the requested pixels and does not send a wheel. Synthetic trees, not a live Chrome run (#49) (2026-10-04).

## [0.4.24] - 2026-10-04

### Fixed

- Linux line scroll steps a fixed-height overflow list by its own vertical AT-SPI scroll bar. On the 0.4.23 retest that list was an AXList 1239 by 422 at (20, 139). The wheel inside the box stayed at `mean_abs` 0, AT-SPI `scroll_to` did not leave ITEM-180 painted, and `scroll_to_find` ended on `page_unchanged` (dy=-1, rows ITEM-001 through ITEM-008). Six later dy=-1 audit rows were `ok` while the before and after screenshots both stayed on ITEM-001. The bar is stepped by about three rows per line, and not more than the rows already on screen. A bar whose range matches the rows' extent is stepped in that unit. A smaller range (a fraction from 0 to 1, or one unit per row) is stepped by that same distance as a fraction of the extent. A write that jumps past the request is undone and is not a success. The list pixels and the on-screen head still have to change. No wheel is sent when the bar step moves the list. A list with no such bar still tries `scroll_to`, and that still has to move the pixels. A content-height list and a document group keep the wheel. `unit=pixels` still uses the existing scroll-bar walk and does not send a wheel. Synthetic trees, not a live Chrome run (#49) (2026-10-04).

## [0.4.23] - 2026-10-04

### Fixed

- Linux `scroll_to_find` moves a fixed-height overflow list whose wheel does not. On the 0.4.22 retest that list was an AXList 1239 by 422 at (20, 139). The wheel landed inside the box, `mean_abs` stayed 0, the painted head stayed ITEM-001, and ITEM-180 never appeared (`page_unchanged`). The same wheel still moves a content-height list and a document group. A Chromium list whose own box sits fully on the screen now reveals a later row at the top of the list through AT-SPI `scroll_to` (about three rows per line, and not more than the rows already on screen). No wheel is sent when that reveal moves the pixels. A true no-move stays `page_unchanged`. The body-scroll find, an explicit scroll whose snapshot head is the painted row, and a still page at the bottom of a content-height list are unchanged. `unit=pixels` still writes the AT-SPI scroll bar and does not send a wheel. Synthetic trees, not a live Chrome run (#49) (2026-10-04).

## [0.4.22] - 2026-10-04

### Fixed

- Linux list rows are clipped to the document, not the screen top. On the 0.4.21 retest the body-scroll find held, and the snapshot head was three rows above the paint: ITEM-168 while the first full row was ITEM-171, ITEM-009 after a scroll whose painted head was ITEM-012, and ITEM-174 while the screen showed ITEM-177. The document group is 1271 by 709, so those rows sit in the browser chrome above the page. A row flush with an overflow list's own top is still the head. A fixed-height overflow list is wheeled on its first painted row. 0.4.20 and 0.4.21 wheeled the list box (1239 by 422) and the list stayed on ITEM-001 (`page_unchanged`, `mean_abs` 0). A true no-move stays `page_unchanged` and names the rows on screen. `unit=pixels` still writes the AT-SPI scroll bar and does not send a wheel. Synthetic trees, not a live Chrome run (#49) (2026-10-04).

## [0.4.21] - 2026-10-04

### Fixed

- Linux `scroll_to_find` reports a row that is painted in the window. On the 0.4.20 retest a document-scroll page did move (the wheel hit a document group, not the tab strip) and ITEM-180 was on screen, but the snapshot had no ITEM rows: the AXList origin was about y=-4847, and the row walk treated that content box as the viewport, so it published the rows at the content origin and the pruner dropped them. The same saved rows (ITEM-001 through ITEM-008) were reused for a later still page, because the huge boxes still overlapped. The on-screen top of the list is now the head line. A list whose own top is on the screen is unchanged: a fully visible row flush with that top is the head, and a row above the list or a row that only fills the 8px edge stays omitted. A fixed-height overflow list is wheeled at its own center. 0.4.20 moved that center onto the reported screen, and the list stayed on ITEM-001 (`page_unchanged`, `mean_abs` 0). An explicit ref whose pixels move takes the painted row as the head. A true no-move stays `page_unchanged` and names the rows on screen. `unit=pixels` still writes the AT-SPI scroll bar and does not send a wheel. Synthetic trees, not a live Chrome run (#49) (2026-10-04).

## [0.4.20] - 2026-10-04

### Fixed

- Linux `scroll_to_find` without a ref on a document-scroll page wheels the page, not Chrome's tab strip. On 0.4.19 the anchor treated the largest element as the window. A list whose AT-SPI bounds are the content height is that element, so the filter dropped it and the tab strip, in the same tier, won. The wheel hit the strip, the page stayed at the top, and the call returned not found. The anchor is now the document group that contains that list, or the list itself when the document group is not in the tree. A content-height list is wheeled on the part that is on the screen. A fixed-height overflow list, an explicit ref, and a still page (`page_unchanged`, no new head) are unchanged. Synthetic trees, not a live Chrome run (#49) (2026-10-04).

## [0.4.19] - 2026-10-04

### Fixed

- Linux Chromium snapshot includes a fully visible row whose own top is the list's top. On 0.4.17 and 0.4.18 the on-screen check required that top to clear an 8px sliver below the list, so a row flush with the box (ITEM-001 fully on screen) was omitted and the snapshot's first list row was the next one (ITEM-002). A row that only fills that sliver is still not the head, and a row whose top is above the list is still not the head, including one at y=-2 whose box covers a sample. `scroll_to_find`'s turnaround after a still page is unchanged. Synthetic trees, not a live Chrome run (#46) (2026-10-04).

## [0.4.18] - 2026-10-04

### Fixed

- `scroll_to_find` keeps searching when a wheel does not move the page and the target has not been shown. On the 0.4.17 retest the snapshot head matched the visible rows (ITEM-193 through ITEM-200) and the call returned `unsupported` with `reason=page_unchanged` and `mean_abs` 0.0, while ITEM-180 was above that window and had never appeared. A still page at that end turns the search around, one line at a time, instead of stopping. A second still page, with nowhere left in the other direction, still returns `page_unchanged` and does not install a new head. `unit=pixels` is unchanged. Synthetic driver, not a live Chrome run (#33) (2026-10-04).

## [0.4.17] - 2026-10-04

### Fixed

- Linux line scroll on a Chromium list reads the on-screen rows from the list node the snapshot walk is holding. On the 0.4.16 retest the pixels did move (`mean_abs` about 7.07 and 6.94) but the snapshot head stayed a row above the viewport: ITEM-001 at y=-2 after a 3-line scroll, and ITEM-013 at y=-26 while the first fully visible row was ITEM-023. `scroll_to_find` ITEM-180 returned `unsupported` with `reason=rows_stale`. The 0.4.16 check stored rows on one Python wrapper and the walk read another, so it published the cached child whose own top was above the viewport. That scan also stopped after forty nodes and did not open a wrapper that starts above the list. The walk now keeps going through those rows and opens that wrapper, and the head is the first row whose own top is on or below the list's head line. A grab that stays still is still `unsupported` with `reason=page_unchanged` and does not install a new head. `unit=pixels` is unchanged. Synthetic trees, not a live Chrome run (#33) (2026-10-04).

## [0.4.16] - 2026-10-04

### Fixed

- Linux line scroll on a Chromium list no longer treats a row whose box still covers the sample as the snapshot head when that row's own top is above the list. On the 0.4.15 retest the pixels did move (`mean_abs` about 6.83 and 6.98) and the on-screen head became ITEM-010, but the snapshot head stayed ITEM-001 at y=-2, so `scroll_to_find` ITEM-180 returned `unsupported` with `reason=rows_stale`. The 0.4.15 check only dropped a box that missed the sample. A row at y=-2 tall enough to cover the sample is not the head. Children whose tops are on or below the head line are the rows the snapshot lists. A grab that stays still is still `unsupported` with `reason=page_unchanged` and does not install a new head. `unit=pixels` is unchanged. Synthetic hit tests and solid grabs, not a live Chrome run (#33) (2026-10-04).

## [0.4.15] - 2026-10-03

### Fixed

- Linux line scroll on a Chromium list no longer stops on `rows_stale` when the hit test is still the pre-scroll row parked above the list. On the 0.4.14 retest the pixels did move (`mean_abs` about 6.83 and 6.86) and the on-screen head became ITEM-010, but the snapshot head stayed ITEM-001 at y=-2, so `scroll_to_find` ITEM-180 returned `unsupported` with `reason=rows_stale`. A row whose own bounds do not cover the sample is not the head. The snapshot head is the row inside the list, once that row is stable. A grab that stays still is still `unsupported` with `reason=page_unchanged` and does not install a new head. `unit=pixels` is unchanged. Synthetic hit tests and solid grabs, not a live Chrome run (#33) (2026-10-03).

## [0.4.14] - 2026-10-03

### Fixed

- Linux line scroll on a Chromium list no longer treats the grab taken in the same turn as the wheel as the whole pixel check. On the 0.4.13 retest that one sample was `mean_abs` 0.0, so `scroll_to_find` ITEM-180 and a 3-line scroll both returned `unsupported` with `reason=page_unchanged`, while the list on screen had moved (a later list-region measure was 6.13, then 5.90). The snapshot stayed on ITEM-001, including 5.6 seconds after the call returned, because a rejected scroll does not replace the confirmed rows. The driver now resamples the same list box until the mean absolute difference clears the still-page threshold or the samples run out. A sample run that stays still is still `unsupported` with `reason=page_unchanged` and does not install a new head. A difference is not success by itself: the snapshot head below the top sliver still has to leave the pre-wheel row. `unit=pixels` is unchanged. Synthetic hit tests and solid grabs, not a live Chrome run (#33) (2026-10-03).

## [0.4.13] - 2026-10-03

### Fixed

- Linux line scroll on a Chromium list reports success only when the list pixels move and the snapshot head below an 8px top sliver leaves the pre-wheel row. On the 0.4.12 retest a 3-line scroll moved the screen from ITEM-001 to ITEM-010, but the snapshot head stayed ITEM-001 for 1.5 seconds and was ITEM-009 about 2.7 seconds later. A further wheel whose pixels did not move (mean absolute difference 0, the screen still ended at ITEM-180) returned success, and the next snapshot started at ITEM-192, which was not on screen. `scroll_to_find` ITEM-180 had already passed on that retest (found after 16 scrolls). A still grab is now `unsupported` with `reason=page_unchanged` and does not replace the rows. A grab that changed while the head never leaves the old row is `unsupported` with `reason=rows_stale` and does not replace the rows either. The next snapshot keeps the rows last confirmed for that list box, so a later hit test cannot publish a row that is off screen. `unit=pixels` still writes an AT-SPI scroll-bar value and does not send a wheel. Synthetic hit tests and solid grabs, not a live Chrome run (#33) (2026-10-03).

## [0.4.12] - 2026-10-03

### Fixed

- Linux `scroll_to_find` searches the snapshot, so a Chromium list in that snapshot is the rows a layout hit-test places in the list's box. On the 0.4.11 retest the cached children stayed on ITEM-001, including 1.5 seconds later, while the screen had moved, and a later snapshot head was only ITEM-009 when the screen showed ITEM-018 through ITEM-025. Chromium's first `GetAccessibleAtPoint` answer is that stale bounds guess and starts a renderer hit test; a later call at the same point returns the layout row. The snapshot lists those rows, including one as far down as ITEM-180 once the layout window contains it. A wheel whose layout rows do not change is `unsupported` with `reason=page_unchanged`. `unit=pixels` is unchanged: it still writes an AT-SPI scroll-bar value and does not send a wheel. Synthetic hit tests, not a live Chrome run (#33) (2026-10-03).

## [0.4.11] - 2026-10-03

### Fixed

- Linux `set_value` replaces a Chrome web field. 0.4.9 returned success when `Text.get_text(0, character_count)` equalled the new string, and a field with no EditableText fell through to typing, which appends. A snapshot reads `Text.get_text(0, -1)`. On the 0.4.9 retest the tool said `BETA` and the accessible value was the previous text with the new string appended. The check is now that snapshot read. When it disagrees, the snapshot text is selected and deleted; the editable-text adaptor's true return is not treated as a clear. If the snapshot text is still there, or the field exposes no EditableText, X11 focuses the field, sends ctrl+a and BackSpace, and types the new string. Success requires the snapshot read to equal the new string. GTK `set_text_contents` still replaces and does not delete. The web-field path is covered by synthetic tests on a fake transport, not a live Chrome run (#31) (2026-10-03).
- Linux line scroll accepts a wheel that moves a list. 0.4.9 compared child names immediately and returned `unsupported` with `reason=tree_unchanged` when that read still showed the old rows; the next snapshot started further down the list, and `scroll_to_find` stopped on the first wheel. The signature includes nested labels, the AT-SPI child cache is cleared before the comparison, and a few later reads are accepted when the first refreshed read is still the old list. A list whose names stay the same is still `unsupported`. Synthetic tests on a fake transport, not a live Chrome list (#33) (2026-10-03).
- Linux `hover` is an MCP tool, an `act` step, and a `run-once` name. 0.4.10 already moved the pointer from `Runtime.hover` with no click. `list_tools` did not include `hover`, `call_tool("hover")` was an unknown tool and left the pointer where it was, and `run-once` rejected the name. Those three call the existing runtime method: move the pointer to x,y or a ref and deliver a hover with no button. A point owned by another app is still `focus_changed` and the pointer is not moved. On any driver other than Linux the tool returns `unsupported` and does not move the pointer. Synthetic tests, not a live tooltip run (#37) (2026-10-03).

## [0.4.10] - 2026-10-03

### Fixed

- Linux hover moves the pointer and delivers the motion with no button. `mouse_move` and `move` had only remembered the coordinate, so the pointer stayed put and a tooltip or a hover-opened menu could not be driven. Click, right-click, double-click, and drag are unchanged (#35) (2026-10-03).

## [0.4.9] - 2026-10-03

### Fixed

- Linux `set_value` replaces a web text field. Chromium's `set_text_contents` appends and still returns true, so ALPHA then BETA left the concatenation; the field is cleared and the new string inserted when the read-back disagrees. GTK `set_text_contents` already replaces, and that path does not delete (#31) (2026-10-03).
- Linux `google-chrome` resolves to the running `chrome` process, and granting the display name "Google Chrome" stores that same id. A snapshot of the launcher name no longer comes back empty with a `screen_text` hint (#32) (2026-10-03).
- Linux `scroll` with `unit=lines` on a named list does not report success when the row names are unchanged after the wheel. Pixel scroll still writes the scroll bar's value by the requested pixels (#33) (2026-10-03).

## [0.4.8] - 2026-10-03

### Fixed

- Linux menu shortcuts prefer a bare named accelerator (`F11`, `F1`, `Delete` list as `f11`, `f1`, `delete`) over the leading mnemonic letter. A tagged chord such as `<Primary>n` is still `ctrl+n`, not the Alt mnemonic (#29) (2026-10-03).

## [0.4.7] - 2026-10-03

### Fixed

- Linux `menu` close sends Escape and returns success only after the popup is gone. Pressing the menu-bar entry again left the GTK menu open while the tool reported it closed (#26) (2026-10-03).
- Linux menu shortcuts use the accelerator in an AT-SPI binding (`n;<Alt>f:n;<Primary>n` is `ctrl+n`), not the Alt mnemonic (#27) (2026-10-03).

## [0.4.6] - 2026-10-02

### Fixed

- `find` and `scroll_to_find` match the field's full text, not the 200-character snapshot clip (#17) (2026-10-02).
- Linux `scroll` with `unit=pixels` sets the accessible scroll bar's AT-SPI value by the requested delta and keeps the write when the value reads back as that delta. GTK scrolled windows expose the bar's value in pixels, so a request of 3 pixels is no longer one wheel notch per count (the 0.4.5 behaviour #18 reported on Mousepad, where both units moved the scrollbar by about 175). A shorter move is kept only when one more pixel does not move (the bar's end; GTK often reports `upper` while the visible end is `upper - page_size`). `unit=lines` is still one X11 wheel notch per unit. A pixel scroll with no scroll bar, or a write that jumps or does not stick, is `unsupported` and does not send notches, so a success string says "pixels" only after that read-back (#18) (2026-10-02).
- Launching a program that is not on `PATH` and has no desktop file fails immediately instead of reporting success after 60 seconds (#19) (2026-10-02).
- An unknown key chord over MCP is a validation error, not an internal crash (#20) (2026-10-02).
- Linux `menu` list, press, state, and close walk the AT-SPI menu bar. `file_dialog` stays unsupported, and the tool result says GTK and portal file choosers are not driven by this tool (#21) (2026-10-02).

## [0.4.5] - 2026-10-02

### Changed

- Linux install docs now say the package is on PyPI and list the source-build packages (#15) (2026-10-02).

### Fixed

- The MCP server reports this package's version instead of the mcp library version (#15) (2026-10-02).

## [0.4.4] - 2026-10-02

Issue #13, filed by Codex against 0.4.3: a window on another Space is not capturable, and the fallback showed the wrong app.

### Fixed

- App-scoped OCR never falls back to a display crop on macOS: `screencapture -l` fails for a window on another Space (or minimized), and the crop at that window's rect returned whatever the user had there, Codex's chat in #13. The result is now a structured `unsupported` with `reason=window_not_capturable`; the accessibility tree still reads. Drivers without window capture keep the crop (#13) (2026-10-02).
- `window list app=X` lists X's windows on other Spaces too, each row carrying `on_screen`, so it agrees with `desktop_snapshot`, which reads an app's focused window even off-Space (`AXMainWindow`/`AXFocusedWindow` answer while `AXWindows` is empty) (#13) (2026-10-02).

### Not reproduced

- "Chrome snapshot shows toolbar only": on the current build a Chrome window off-Space snapshots with its page controls (17 radio buttons, 14 buttons, 8 popups, links on a settings page), and Chrome's enhanced accessibility was already on. Left open in #13 with a request for the page and the snapshot header on the next occurrence.

## [0.4.3] - 2026-10-01

Issue #12, filed by Codex: the 31-second OCR was macOS, and the fix makes captures faster than they have ever been.

### Fixed

- Screenshots and OCR took a flat 30 s per call on macOS 26.6: the deprecated in-process `CGWindowListCreateImage` now times out at 30 s and `CGDisplayCreateImage` returns nothing. Display capture goes through `screencapture` (ScreenCaptureKit) first, 0.3 s measured; the Quartz path stays behind `A11Y_COMPUTER_USE_CAPTURE=quartz` (#12) (2026-10-01).
- `screen_text(app=X)` and the automatic OCR escalation capture X's own window (`screencapture -l`, shadow omitted) instead of cropping a display capture: whatever covers the window stays out, and a window on another Space is captured too. Measured: `screen_text(app=Chrome)` 31 s and another app's text before, 0.8 s and 40 lines of Chrome's page after, with Chrome on another desktop (#12) (2026-10-01).
- `set_value` focuses the element after writing it, so a following `key(chord="return", app=...)` lands in that field (an address bar) instead of nowhere (#12) (2026-10-01).

### Not yet

- Per-window pixels for the `screenshot` tool itself, snapshot latency on large Chrome trees, and a Chrome tree that exposes page content on the first snapshot (the enhanced-accessibility unlock exists; whether Chrome needs it flipped earlier is open). #12 stays open for those.

## [0.4.2] - 2026-10-01

Issue #11, filed by Codex: "focused" was not "visible".

### Fixed

- A ref click no longer runs the pointer hit-test: an AXPress addresses the element, so a floating window over the target (Codex over Chrome, #11) no longer turns it into `focus_changed`; synthesized mouse events keep the guard right before injection, and a bound browser tab keeps its tab-switch guard on every path (2026-10-01).
- `app focus` says when the app's window is covered: after activation it hit-tests the centre of the app's first window, and reports the covering app instead of a bare "focused", since a frontmost app under a floating window is not visible to the user (#11). Closes #11 (2026-10-01).

## [0.4.1] - 2026-10-01

From the first Codex session on After Effects: two agent-filed issues (#9, #10), the owner's two complaints (an ugly permission dialog; the agent fighting the user for the app), and the fixes.

### Fixed

- `file_dialog` typed the path through the HID tap, which goes to whatever app is in front, and reported success while the go-to-folder sheet kept an earlier path (#9). Open and save panels live in AppKit's `openAndSavePanelService`, a separate process: keystrokes are now addressed to the panel's own process, the sheet field is read back before Return (retried with a direct AXValue set), and a persisting mismatch is a structured `unsupported` with `reason=dialog_unchanged` instead of a report of success. Closes #9 (2026-10-01).
- `type`/`key` with `app=X` now go to the process that owns X's focused element when that differs from X (the panel service), so typing into an app's open or save panel works by address (2026-10-01).
- `set_value` no longer fails a pointer hit-test it does not need: an AXValue write lands on that element only; the typing fallback is addressed to the element's process, and only the last-resort HID typing keeps the classic guard (#9) (2026-10-01).
- `screen_text(app=X)` and the automatic OCR escalation never read the whole display under X's grant: with no window rect known (X on another Space, minimized, or not yet open) `screen_text` is a structured `unsupported` with `reason=app_not_on_screen`, and the escalation says it skipped OCR. The whole-display fallback returned another app's text (#10). Closes #10 (2026-10-01).
- The MCP server refuses to fight the human for the machine: `app focus`, `app quit`, `window raise`, HID input (coordinate clicks, drags, scrolls, frontmost typing), and keystrokes addressed to the app the user is currently in return `user_active` while the user's last hardware mouse or keyboard event is younger than `A11Y_COMPUTER_USE_USER_IDLE_S` (1.5 s); addressed keystrokes into an app the user is not in are unaffected. Our own synthesized input never counts as the user's. New wire-stable error code `user_active` (2026-10-01).

### Changed

- The native confirmation is a real macOS alert (`a11y_computer_use._alert`, an `NSAlert` in a short-lived helper process): the project icon, a headline such as "Allow ChatGPT to control After Effects?", one short paragraph, the details in a scrollable box, Allow and Don't Allow, and for issue reports a "Always allow issue reports from this tool" checkbox remembered in `~/.a11y-computer-use/settings.json`. AppleScript's `display dialog` remains the fallback. `grant_app` names the host app and the app's display name (2026-10-01).

## [0.4.0] - 2026-10-01

The user keeps working while the agent works. The same answer as the cursor: address the app, not the screen.

### Added

- `type(text, app=X)` and `key(chord, app=X)`: keystrokes addressed to X's process (`CGEventPostToPid`) reach its key or main window without activating it, so the user's screen and Space stay put. Gated against X (tier `full`), with a recheck that the process is still the one the grant was decided for, a stronger guarantee than the frontmost check. Verified live on TextEdit while another app had focus. Pointer events cannot be addressed this way (AppKit drops them without a window), so coordinate clicks, drags, and wheel scrolls still need the app in front; the tool docs say so. macOS only; `unsupported` elsewhere (2026-10-01).
- `app launch ... activate=false` (`open -g`): the app starts behind the current one. `A11Y_COMPUTER_USE_FOCUS_MODE=background` makes both the default: `type`/`key` without `app` address the app of the latest snapshot, and launch does not activate. `docs/coexist.md` states what never activates, what still does, and the Space limit: the accessibility API only exposes windows on the user's current Space, so an app on another desktop or behind a fullscreen app cannot be observed until they share a desktop again (2026-10-01).
- The server instructions tell planners to prefer refs, `set_value`, menus, and addressed keystrokes over `app focus` and coordinate clicks, and to say so before a focus change (2026-10-01).

### Fixed

- `app launch` no longer waits a minute for a window that opened on another Space: the wait also queries windows on every Space (`kCGWindowListOptionAll`), where the on-screen list and the accessibility API stop at the current one (2026-10-01).

## [0.3.2] - 2026-10-01

Adoption: the two grants come to the user instead of the user hunting for them, and agents report the tool's own defects where they get fixed.

### Added

- One-step macOS grants: the first `permission_denied_accessibility` or `permission_denied_screen` in a process asks macOS to show its own grant dialog (`AXIsProcessTrustedWithOptions` with the prompt option, `CGRequestScreenCaptureAccess`), opens the exact System Settings pane, and names the host app in the hint (for Codex that is ChatGPT.app, which nobody could guess). `request_permission(kind)` repeats that and waits up to 90 s for the switch. `A11Y_COMPUTER_USE_NO_OS_PROMPT=1` keeps the dialog closed. `docs/permissions.md` (2026-10-01).
- `grant_app(app, tier)`: an ungranted app is granted through the host's own confirmation dialog (MCP elicitation) and recorded on accept; hosts without elicitation get the shell command instead, and nothing is recorded without a human's yes. `needs_permission` refusals now carry both routes. `a11y-computer-use grant <app> <tier>` (list with no arguments, `--revoke`) for the shell (2026-10-01).
- `report_issue(kind, title, body, tool)`: agents file defects, bottlenecks, missing capabilities, and app-compatibility gaps on the project's public issues, with secrets, e-mails, and the home directory redacted and an environment table appended; the host confirms first, `gh` files it under `agent-report` plus the kind, and without `gh` or a confirmation channel the tool returns a prefilled link. The server instructions say when to report; a crash inside a tool reads as `internal_error` with the report instruction, and a call over `A11Y_COMPUTER_USE_SLOW_CALL_S` (10 s; waiting tools exempt) is marked `[slow call]`. `docs/reporting.md`, issue template `agent_report.yml`, labels (2026-10-01).
- `a11y-computer-use --version`; `__version__` reads the installed distribution instead of a stale constant (2026-10-01).
- A native macOS confirmation dialog, shown by the server itself, when the host cannot show one: Codex's MCP client has no elicitation, so `grant_app` had answered "the user declined" for a prompt nobody saw. `grant_app` and `report_issue` now try the host's dialog, then a dialog from this process (System Events, which the tool's tiers never grant, so the agent cannot click it), and only then hand back the shell command (2026-10-01).

### Fixed

- The MCP server never saw an app launched a moment ago (Calculator, Notes: "no window appeared within 60s", `app_not_found` right after `launched`): the NSWorkspace refresh only ran on the main thread, and the server runs every tool on a worker thread. The refresh now hops to the main thread's loop, and app lookup falls back to a direct LaunchServices query by bundle id (2026-10-01).

### Changed

- `tool_specs` (the local agent loop's planner surface) leaves out the host tools `request_permission`, `grant_app`, and `report_issue`, which need the host's confirmation channel (2026-10-01).

## [0.3.1] - 2026-09-20

Found by four planner trials on the Box Linux desktop over the remote backend (Krita, Excalidraw in Chromium, ffmpeg in a terminal, gedit): the "Mona Lisa" the first run reported was a single dot, every screenshot took 6 to 70 s, launches waited a minute for a window that was already up, quit did nothing, the document and the terminal screen were missing from snapshots, and the first `app list` on a fresh desktop was refused.

### Fixed

- Linux `drag` walks its waypoints: the driver had dropped the path and sent one press, one jump, one release, which a freehand brush paints as a dot. The stroke is now 8 px hops through every waypoint, each flushed and paced, endpoint exact; a 15-waypoint circle renders as a circle in Krita 5.2.2 on the Box (6ec0466, 2026-09-20).
- Linux app resolution prefers the owning process over a window that merely names it: a Chromium tab "Donations | Krita" stacked above Krita had turned `window list app=krita` into Chromium's rows (fd8b41b, 2026-09-20).
- `app launch` off macOS recognises the window it opened: window rows carry the process comm, cut at 15 bytes ("gnome-terminal-") or without the vendor prefix ("chrome" for google-chrome), which never equalled the launched name; the id is also re-resolved each poll. `app quit` sends the driver's own chord (ctrl+q on Linux, alt+f4 on Windows) instead of cmd+q, which is Super+q on X (129c51b, 2026-09-20).
- GTK notebook pages with a hidden tab label (gedit with one document, gnome-terminal with one tab) keep their content: GTK reports a negative extent for the page tab, the reader had turned that into "no extents" and the engine dropped the subtree. The AT-SPI "terminal" role reads as a text area, so a VTE screen is snapshot text. Zero-size and offscreen nodes still drop, so macOS trees are unchanged (17420da, 2026-09-20).
- `app list` on a fresh desktop: the frontmost "app" is the desktop shell, which nobody grants. The list (identities only) is gated against the frontmost app when granted, else a trusted running app, else any app the human has granted on the machine; with no grant anywhere it still refuses (724bab0, c12ee13, 2026-09-20).
- Windows: two processes opening the empty lock sidecar together no longer abort on the second one's `EACCES` write into the first one's locked byte; the byte exists by then and the lock loop waits as usual. Seen twice on the GitHub Windows runner in `test_safety_persistence`; the 5 s replace wait in ba89839 was aimed at the wrong call (a8a254f, 2026-09-20).

### Added

- `wait_until {"settle": seconds}`: the one condition with nothing to observe, for apps with no accessibility tree that are still loading or animating; the Krita planner had been waiting on `file_stable ~/.bashrc` to get the same effect (fd8b41b, 2026-09-20).
- `agent --view-images`: a claude-cli planner looks at its screenshots (the newest one is written to a temp PNG the CLI can Read) instead of a text description; needed for apps with no accessibility tree. API providers always send images inline (4688406, 2026-09-20).
- `scripts/box/README.md` documents driving a Box from a local planner with `--mcp-command`, the grants, the session env, and this image's Qt accessibility limit: Krita 5.2.2 and a bare PyQt5 window never register on the AT-SPI bus there, so Qt apps are driven by screenshot and coordinates (e8ca239, 76aaf91, 2026-09-20).

- `screenshot(format="jpeg", quality=80)` on the MCP tool: the same pixels about five times smaller. `agent --mcp-command` asks a remote server for JPEG when its schema advertises the argument (older servers never see it; `A11Y_COMPUTER_USE_REMOTE_IMAGE_FORMAT=png` opts out) and hands the planner PNG as before. The Box grab and encode take 0.1 s; the 800 KB PNG on a 170 KB/s link was the cost. Measured on that link, two samples each: PNG 29 and 57 s, JPEG 13.6 and 9.5 s (21878e3, 2026-09-20).

## [0.3.0] - 2026-09-20

Instant voice control, a remote desktop backend, and WebMCP tools. The voice pipeline was measured live on a Mac (local router about 60 ms from transcript to action); the remote backend is exercised by CI against a local server and was used live to drive a Linux desktop VM over SSH; WebMCP is covered by scripted-transport tests and one opt-in headless-Chrome test.

### Added

- Reflex layer: one fixed skill per spoken command instead of a planning loop. Nine built-in skills (open app, new document, set title, type text, web search, open URL, take photo, screenshot, menu item) run over the gated `Runtime` with slots pulled from the transcript by regexes, so no model writes text or coordinates. `LocalRouter` routes on patterns with no network; `JevRouter` asks TypeSafe System One for one choice over the skill list, times out at 2 s, and falls back to the local router (2199647, 2026-09-20).
- `a11y-computer-use voice [--router local|jev] [--text ...] [--push-to-talk] [--json]`: on-device speech through Apple's Speech framework, one timeline per command (`[stt 412 ms] [route 0 ms local] [act 62 ms] open_app ... -> ok`); `--text` runs the pipeline from a transcript without a microphone; `doctor` reports the Speech Recognition and Microphone grants. New macOS dependencies `pyobjc-framework-Speech` and `pyobjc-framework-AVFoundation` (9507d1d, 2026-09-20).
- WebMCP on the browser backend: tools a page registers through `navigator.modelContext` (native, a recorder shim, or declarative `form[toolname]`) appear as refs `w1..wN` under `webmcp tools:` in a browser snapshot; the `webmcp` tool lists them (tier read) or calls one (tier click, lifted to full for free-text, payment, or submission tools; destructive names go through the confirmation gate; arguments are redacted in the audit log). `docs/webmcp.md` states the standard's status and the limits (450aaec, 2026-09-20).
- `a11y-computer-use agent --mcp-command "<cmd>"`: the planner runs locally while every observation and action happens on a remote a11y-computer-use MCP server spoken to over stdio, for example an SSH channel into a Linux desktop VM, under that machine's own grants and audit log. Structured errors keep their codes across the wire, refusals come back as tool text, screenshots as images. `scripts/box/mcp-session.sh` starts the server inside a running X session with the Qt and GTK accessibility bridges on (7d4af2f, 2026-09-20).

### Fixed

- `app focus` no longer burns its whole 5 s wait when NSWorkspace lags the WindowServer stacking order (5077 ms to about 10 ms); the workspace's running and frontmost lists are refreshed with one non-blocking run-loop pass, so an app launched a second ago is seen; "Notes" no longer resolves to the faceless widget extension of the same name; menu queries cache the app's accessibility element per pid (2199647, 2026-09-20).
- Remote wire: an ungranted unknown app is a structured `app_not_found` on macOS and a `needs_permission` refusal text on Linux and Windows; the test accepts both (091558b, 2026-09-20).

### CI

- Live TextEdit menu tests skip under GitHub Actions, whose Macs have no interactive session (fcdcc49, 2026-09-20).
- `tests/test_remote.py` is committed next to its module; an earlier commit had tracked it alone (11ece1c, 8f0668c, 2026-09-20).

## [0.2.1] - 2026-09-19

Fixes from the first live desktop trials and the first gauntlet benchmark run.

### Fixed

- Ref re-resolution is bound to the element's text: a titled ref never resolves onto whatever element now occupies its old position after a live reorder; it follows the title anywhere in the tree, or returns `stale_ref` with `reason=title_changed`, closest-title candidates, and the element at the old position flagged `at_old_position`. Slot-based stable ids no longer beat the title on rows, cells, items, links, and static text. Found by the gauntlet: 18 of 20 Fable runs clicked the wrong row after the list reordered (8f944d4, 2026-09-19).
- Observations of a granted app (`screen_text(app=...)`, `window list app=...`, the automatic OCR escalation) gate against that app, not against whatever app is frontmost (06507e4, 2026-09-19).
- `app launch` by display name gates on the installed bundle id and waits up to `A11Y_COMPUTER_USE_LAUNCH_WAIT_S` (default 60) for the first window; `agent --grant` accepts an installed app that is not running yet (5179328, 9735d77, 06507e4, 2026-09-19).
- `app focus` also trusts the WindowServer stacking order and waits through Stage Manager (83ade08, 2026-09-19).
- A locked screen or sleeping display is a structured `unsupported` error from `agent` and `mission run`, checked only for the real macOS driver; `capture.displays()` raises a structured error instead of `RuntimeError`; a crashing tool becomes an `internal_error` tool result instead of ending the run (864d9dd, d56c535, 2026-09-19).
- Menu-bar helpers read as `unsupported` off macOS, so the platform-neutral seam used by the server tests never imports pyobjc there (0c13c14, 2026-09-19).

### Changed

- `agent` and `mission run` hold the display awake with `caffeinate -dimsu` for the run (`A11Y_COMPUTER_USE_KEEP_AWAKE=0` opts out) (864d9dd, 2026-09-19).
- The agency-demo mission edits the video with ffmpeg in Terminal instead of After Effects (e67803f, 2026-09-19).
- Live TextEdit tests skip on runners that cannot bring an app to the foreground (4667dcf, b2258e2, 7ad1078, 2026-09-19).


## [0.2.0] - 2026-09-19

The desktop release: apps without an accessibility tree, long multi-app missions, menus and file dialogs. The OCR path, TextEdit menus, and Figma's tree were verified live on a Mac; the agency mission in `docs/missions/` has not been run end to end.

### Added

- OCR refs: `screen_text` reads the display with on-device OCR (Apple Vision) and returns text lines as refs `o1..oN`; `click`, `scroll`, `drag`, `wait_for`, and `find(ocr=true)` accept them, re-reading the screen at act time and re-finding the text nearby (`stale_ref` with candidates otherwise). A snapshot with no actionable element appends the OCR lines automatically (`A11Y_COMPUTER_USE_AUTO_OCR=0` disables); `screenshot(marks=true)` marks `o` refs in blue. macOS only; `unsupported` elsewhere. New dependency `pyobjc-framework-Vision` on macOS. Measured on a 3024x1964 Retina display: about 100 ms capture, 465 ms median recognition (b8903be, 2026-09-19).
- `notes` and `wait_until` tools: an agent scratchpad that survives context compaction, and waits for files, URLs, snapshot text, or on-screen text up to 30 minutes (b61a28f, 2026-09-19).
- Agent loop: history compaction past a token budget, `compactions` in the result, and a `deadline_s` stop (b61a28f, 2026-09-19).
- `a11y-computer-use mission run|validate`: long multi-app tasks as verified phases with per-phase grants, retries, runner-side checks, and a wall-clock timeline; ships `examples/missions/agency-demo.toml` (15afbe0, 2026-09-19).
- `menu` tool: list or press a menu item by path (`File > Export > Add to Render Queue`) through the accessibility menu bar; opens each level and re-reads items on open; destructive labels ask for confirmation; macOS only, structured `unsupported` elsewhere (8e7c5e5, 05bc300, 2026-09-19).
- `file_dialog` tool: drive the frontmost open or save panel to an absolute path via the go-to-folder sheet (macOS, hermetically tested) (8e7c5e5, 2026-09-19).
- `app launch` waits for the first window and returns its title; `app focus` waits until frontmost; `app quit` (tier full) sends cmd+q and reports a lingering dialog (8e7c5e5, 2026-09-19).
- `drag` accepts `path=[[x,y],...]` waypoints, one continuous stroke through a path, for painting and gesture input; `act` steps take it too (8bc570a, 2026-09-19).
- `menu(action="state"|"close")` reports and dismisses an open menu; `click`, `type`, and `key` close an open menu in the gated app before acting and say so; `desktop_snapshot` shows `open menu: File > Font` in its header. Open menus swallowed key chords during the live trials (d4615d8, 2026-09-19).
- Auto-OCR escalation and `screen_text(app=...)` crop to the app's windows instead of reading the whole display; the fallback to the full display is stated in the reply (d4615d8, 2026-09-19).

### Fixed

- Electron apps (Figma, VS Code) at app scope exposed nothing: their windows hang off `AXWindows`, `AXMainWindow`, and `AXFocusedWindow` rather than `AXChildren`, and the application element has no geometry, so the walk dropped the subtree. Figma now exposes its panels (78 elements, 20 actionable, 0.66 s) (7cf8406, 2026-09-19).
- `app focus` claimed success when the app never came to the front; it now escalates through `open -b`, `AXFrontmost`, and `AXRaise`, verifies, and raises `focus_changed` naming the app actually in front (b23370b, 2026-09-19).
- A direct `AXPress` on a deep menu item reported success while doing nothing, and Escape did not end AX menu tracking, leaving the app answering every call at the messaging timeout; menus are opened level by level and closed with `AXCancel` (05bc300, 2026-09-19).
- `wait_until` URL checks refuse hosts that resolve to loopback, private, link-local, multicast, or reserved addresses and never follow redirects, so a planner steered by page content cannot probe internal services; `A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS=1` opts out for local servers (c33ea11, 2026-09-19).

### Docs

- `docs/ocr-refs.md`, `docs/missions.md`, `docs/macos-primitives.md`, and `docs/missions/agency-demo.md`, the mission that drives this release (0915003, 2026-09-18).

## [0.1.1] - 2026-09-18

### Changed

- Renamed to `a11y-computer-use`: PyPI rejected `computeruse` and `computeruse-mcp` as too similar to reserved names. The import is `a11y_computer_use`, the CLI and MCP server are `a11y-computer-use`, environment variables use the `A11Y_COMPUTER_USE_` prefix, state lives in `~/.a11y-computer-use/`, and the repository moved to Perception-Dynamics-Inc/a11y-computer-use. First release on PyPI (388cc84, 2026-09-18).
- Release workflow: a `v*` tag builds, smoke-tests, and publishes through PyPI trusted publishing (eb8e393, 2026-09-18). GitHub Actions bumped to v7 (fb1c0e5, 2026-09-18).
- README cut to about a hundred lines; PLAN.md and the July decision records moved under `docs/decisions/` (1e30314, 2026-09-18).

### Hardening (production-hardening branch, merged 2026-09-04)

- Runtime operations and batches retain exclusive ownership of their snapshot;
  MCP admission and queue waits are bounded, with `busy` and `closed` errors.
- CDP commands use serialized deadlines, strict tab binding, bounded diagnostic
  buffers, explicit disconnect errors, and release remote DOM handles.
- Permission updates are atomic across local processes; invalid policy edits
  deny actions. Audit files have configurable size/retention limits and recover
  incomplete writes.
- Windows permission updates retry transient file-sharing errors within a
  deadline and use consistent file metadata to cache unchanged policies.
- Browser password probes fail closed. Long scrolling searches recheck their
  target and permissions before input.
- Linux typing verifies the remembered editable belongs to the frontmost app
  and clears stale targets after focus-changing actions.
- Linux XTEST typing prepares the full Unicode keymap before input and paces
  keystrokes consistently to prevent the observed GTK/IBus character reordering.
  Insufficient spare keycodes now reject the text before emitting partial input.
- ASCII Box verification fails on missing live coverage, preserves reports,
  and includes a verified multiprocess browser load harness. See
  [production guidance](docs/production.md) and [concurrency contract](docs/concurrency.md).

### Fixed

- The permission store notices a repaired policy file even when size and mtime did not change: while the last load failed, a content digest is compared as well, so a same-length rewrite within one filesystem timestamp tick (Windows) is picked up (230b5f2, 2026-09-18).

## [0.1.0] - 2026-09-02

The first tagged release. Everything the project has shipped so far lands here, since nothing was tagged before it. Install from a clone of the tag; the package is not on PyPI.

### Added

#### Repository

- Repository created with an MIT license and a two-line README (25266b1, 2026-07-02).

#### Observe, act, and the MCP surface

- Phase-0 MVP: the a11y-first macOS framework. Observe (pruned accessibility snapshots with `e1..eN` refs), act (CGEvent input), safety (permission tiers, per-app grants, JSONL audit log), screen capture, `doctor`, the MCP server, the `a11y-computer-use` CLI, the test suite, and the demand-validation record in `docs/` (96a70b9, 2026-07-12).
- Automatic a11y to vision handoff: a full snapshot with no clickable or editable element appends a note pointing the agent at `screenshot`. Per-widget pruning caps dense containers (grids, tables, outlines) at 12 children instead of the general 24 (da96084, 2026-07-13).
- `Driver` protocol and the `a11y_computer_use/drivers/` package. The platform-free core (observe engine, safety, Runtime, MCP server) reaches native APIs only through this seam. Shipped with the macOS driver and a Windows skeleton whose methods raised `NotImplementedError` naming the native API each would use, plus `docs/windows-port.md` (81d3b19, 2026-07-13).
- `find` tool (filter a fresh snapshot by text, role, editable, or clickable), richer element states in the snapshot text, and force-enabled Chromium/Electron accessibility trees on macOS by setting `AXManualAccessibility` and `AXEnhancedUserInterface` on the app element (opt out with `A11Y_COMPUTER_USE_NO_WEB_A11Y`), with `examples/web_a11y_demo.py` (950ae21, 2026-08-29).
- Stable-id anchors: when the app exposes a stable identifier, a ref re-resolves by it before falling back to role, title, path, and bounds proximity. At HEAD the sources are `AXIdentifier` (macOS), `AutomationId` (Windows), `accessible-id` (Linux), and the backend DOM node id (browser) (ce02cb0, 2026-08-29).
- Batched `act` tool: a list of click, type, key, scroll, drag, and wait_for steps in one call. Each step is gated and audited at its own tier. The batch stops at the first failing step and does not roll back steps already executed (4bd1886, 2026-08-29).
- Diff snapshots: `desktop_snapshot(mode="diff")` returns only the added, removed, and changed elements against the previous snapshot of the same app, and falls back to the full render when there is no such snapshot (4855216, 2026-08-29).
- Self-correcting `stale_ref` errors: when a ref no longer resolves, the error detail carries up to three near-miss candidates from the live tree (70b2a3a, 2026-08-29).
- `set_value` tool: sets an editable element's value in one accessibility operation, falling back to focus plus typing when the driver cannot set it directly. Secure fields are refused before gating (ea12128, 2026-08-29).
- Set-of-Mark screenshots: `screenshot(marks=true)` draws ref labels from the latest snapshot onto the image (37b2706, 2026-08-29). As first landed, `marks_for` called a nonexistent `ScaledImage.to_scaled` and raised `AttributeError` on the live path whenever there was an element to mark; fixed in 40973f2 (see Fixed).
- `scroll_to_find` tool: scrolls a view and re-observes after each step until a text or role match appears, up to `max_scrolls` (default 6) (61f7fc0, 2026-08-29).
- Effect Receipts: `verify=true` on `click` and `act` re-snapshots the app after the action and appends the diff (0ec4c70, 2026-08-29).
- Interactive snapshot view and token budget: `desktop_snapshot(mode="interactive", budget=N, include_bounds=...)` renders the same snapshot down to input-taking and stateful elements, rows/tabs/sliders that are targets themselves, and the containers that keep them apart (roots, windows, dialogs, menus, toolbars, tab groups, titled groups), with static text folded into one `text:` line per container; refs are unchanged, so acting and re-resolving work across views. `mode="diff"` and Effect Receipts render in the view last asked for, and the interactive diff keeps static-text changes as one `~ text:` line. `budget` cuts any rendering deterministically after the header and reports the omitted element lines. Full-mode output is byte-identical to before (99b8d0b, 2026-09-02).
- `scroll_to_find` takes an optional `ref` that pins the container to scroll (d6c19d9, 2026-09-02).

#### Safety

- Confirmation gate for clicks on destructive labels (delete, trash, discard, erase, and similar) through MCP elicitation. Without an elicitation channel the click is blocked; `A11Y_COMPUTER_USE_CONFIRM=0` disables the gate (ca5adba, 2026-07-12).
- `type` probes for a focused password field on every driver: the browser evaluates `document.activeElement` through open shadow roots and same-origin iframes, Linux walks the active window's AT-SPI tree for the focused node (bounded to 400 nodes), Windows asks UIA for the focused control's `IsPassword`; UIA password edits are marked `AXSecureTextField` and their value is never read. `tests/test_safety_hardening.py` (57 tests) covers the new paths (efd9d81, 2026-09-02).

#### macOS backend

- Ref clicks activate elements through the AX API (`AXPress` and related actions), so the user's cursor does not move; synthetic mouse events remain the fallback (1f27621, 2026-07-12).
- Visual presence overlay: a blue screen-edge glow and an agent cursor in a standalone `a11y_computer_use.overlay` module with a demo (`python -m a11y_computer_use.overlay`). macOS-only; not wired into the MCP server (0b8eab0, 2026-07-13).
- Non-intrusive scroll: `scroll(ref, into_view=true)` uses `AXScrollToVisible` instead of a wheel event (e3231f0, 2026-07-13).

#### Windows backend

- `snapshot` through UI Automation into the shared pruning engine; verified in CI on `windows-latest` against Notepad (6e03c9d, 2026-07-13).
- Act loop: `type_text` via SendInput Unicode, `press_element` via the UIA Invoke, Toggle, SelectionItem, and ExpandCollapse patterns with SetFocus for editables, and `scroll_into_view` via ScrollItemPattern (8ed00dc, 2026-07-13).
- `key_chord` via virtual-key SendInput (ebb66fc, 2026-07-13).
- The full gated Runtime (permission check, recheck, audit) runs on Windows, with app identity taken from the process image name (a865706, 2026-07-13).

At HEAD the Windows driver still raises `NotImplementedError` for `resolve_ref`, coordinate `click`, `drag`, `scroll`, `wait_for`, `screenshot`, `zoom_region`, app and window enumeration, launch and activate, and clipboard read and write.

#### Linux backend

- Linux backend over AT-SPI2: snapshot, ref re-resolution, press, focus, `set_value`, and typing through `EditableText`, with XTEST for coordinate input and EWMH for windowing. Live-verified in CI against a GTK3 window under Xvfb. Ships with the `[linux]` extra, `docs/linux-port.md`, and a `linux` CI job (445dd60, 2026-08-23).
- `org.a11y.Status` is switched on over D-Bus, so running Chromium/Electron apps expose their AT-SPI trees without a relaunch (53cce34, 2026-08-29).
- ARIA `xml-roles` mapping for web roles, with a single attribute fetch per node (f3c997c, 2026-08-29).
- Wayland-native screen capture through `grim`, and `capture.py` now imports cleanly off macOS (615e939, 2026-08-29).
- Structured `unsupported` error for coordinate and key injection on native Wayland, where XTEST is unavailable. The AT-SPI path (press, `set_value`, typing into a field focused through the driver) keeps working (e0d48e3, 2026-08-29).
- `doctor` gains Linux and Windows sections (display session, window manager, AT-SPI bindings and bus, XTEST availability, clipboard tool; the UI Automation import on Windows) instead of reporting macOS grants that do not exist there. AT-SPI application lookup matches by PID as well as name and ranks the active top-level frame first, so a windowless registrant with the same comm never shadows the real app. XTEST typing maps F13 to F24, named punctuation, and control characters, and binds spare keycodes for characters the layout lacks (the xdotool approach), so off-keymap Unicode round-trips. `tests/test_linux_desktop_live.py` adds ten real-desktop tests (coordinate click, scroll, drag, chords, typing, apps, windows, clipboard, the gated Runtime) that run under a window manager and skip without one (5267da2, 2026-09-02).
- `scripts/box/`: `bootstrap.sh`, `run-live.sh`, `verify-pointer.sh`, and `pointer_probe.py` stand up a Box (box.ascii.dev) Ubuntu desktop VM and run the live suites, the browser suite against a non-headless Chrome, and the pointer probe there; `docs/box-testbed.md` records the runs (fada77e, 2026-09-02; 6240b56, 2026-09-02).

#### Browser backend (CDP)

- `BrowserDriver` over the Chrome DevTools Protocol: `Accessibility.getFullAXTree` and `DOMSnapshot` fused into the shared observe engine; coordinate-free press, focus, `Input.insertText`, and `Input.dispatchKeyEvent`. Selected only by `A11Y_COMPUTER_USE_DRIVER=browser` or `get_driver("browser")`, never by platform. Adds the `[browser]` extra (websocket-client), `docs/browser-backend.md`, and the wider package description (58ec018, 2026-08-29).
- Iframe stitching: same-process frames (same-origin, about:blank, srcdoc) are grafted under their owner iframe node with composed offsets. Cross-origin out-of-process frames are skipped rather than failing the snapshot. The walk is capped at 24 frames (18b780b, 2026-08-29).
- Load-aware navigation: `launch_app(url)` runs `Page.navigate` and polls `document.readyState` until it is `complete` (63c3817, 2026-08-29).
- The full gated Runtime runs on the browser backend with tabs as apps: grants are keyed by CDP target id, `app list` and `window list` return tabs, and `app focus` rebinds to a tab (324c919, 2026-08-30).
- `console` tool, browser-only: buffered `Runtime.consoleAPICalled`, `Runtime.exceptionThrown`, and `Log.entryAdded` events returned as `{level, text}`; reading clears the buffer (a132a62, 2026-08-30).
- `network` tool, browser-only: requests joined with their response status or load failure by request id; reading clears the buffer (aa7ceb1, 2026-08-30).
- Hermetic test proving `console` and `network` are registered only on the browser driver: 16 tools on the OS drivers, 18 on the browser (fe279fa, 2026-08-30).

#### Agent loop and provider adapters

- `a11y_computer_use.adapters`: Anthropic (`computer_toolset_20260801` members and the legacy `computer_20251124`/`computer_20250124` action shape, all 17 members, `tool_definition` plus `beta_header`) and OpenAI (GA `{type: computer}` and `computer_use_preview`, the nine actions, `handle_call` running an actions array and building `computer_call_output`) computer-use executors that run provider-native pixel actions through the gated Runtime, with snap-to-ref (smallest actionable element under the point, size cap, `stale_ref` fallback), an optional Set-of-Mark, and a `Result` type that never raises to the host. Adds `docs/provider-adapters.md`, `examples/anthropic_computer_use.py`, `examples/openai_computer_use.py`, and `tests/test_adapters.py` (81 hermetic tests on a recording fake driver plus a scripted-CDP browser run; the live test skips without an endpoint and passed on headless Chrome per the commit message) (d913093, 2026-09-02).
- `a11y-computer-use agent --task`: the reference observe, plan, act, verify loop through the gated Runtime, with pluggable planners in `a11y_computer_use/providers.py` (Anthropic Messages API, OpenAI-compatible chat completions via `OPENAI_BASE_URL` for Ollama/vLLM, the Claude Code CLI with no key, and a scripted provider; stdlib only). The planner sees the real MCP tool schemas (`server.tool_specs`) plus a `done` tool; `stale_ref` errors carry a fresh snapshot; older observations collapse to placeholders. Adds `server.Runtime.call_tool` (the full surface by name; `run-once` dispatch unchanged), `build_server(runtime=...)`, planner token sums in `bench audit`, `docs/agent-loop.md`, `examples/agent_task.py`, 43 hermetic tests, and one opt-in live browser test; live-verified by the author with `claude-cli` on headless Chrome, not in CI (70dcb15, 2026-09-02).

#### Benchmarks and telemetry

- cu-meter: per-action metrics (`duration_ms`, `result_chars`, `tokens_est` at 4 chars per token) written to the audit log, plus `a11y_computer_use/bench.py` to aggregate them (6941bf0, 2026-08-29).
- cu-arena: measures the a11y snapshot cost (chars/4) against the screenshot cost (width*height/750) for the same UI state on the same driver. Adds the `a11y-computer-use bench` CLI with `bench audit` (cu-meter report) and `bench web URL`, and a live step in the browser CI job (6df365e, 2026-08-29).
- cu-arena also scores the re-observe diff cost for every round after the first (9d8d4ae, 2026-08-29).
- `a11y-computer-use bench desktop [--app] [--scope] [--rounds] [--json]` costs every snapshot view (full, interactive) of a running app against the screenshot captured at the same moment, plus the re-observe diff per view, with the same estimator as `bench web`; `bench web` gains `--mode` and `--json`; `snapshot` gains `--mode`, `--budget`, and `--bounds`. A capture the backend cannot deliver is reported as such, never zeroed into a ratio. `docs/observation-cost.md` records the method and the live numbers (example.com 95/72 vs 473 tokens for full/interactive vs screenshot; Hacker News 4,509/758 vs 1,301; re-observe diff 10 tokens on both; Finder not measured, no Accessibility grant in that shell) (910a530, 2026-09-02).
- cu-arena head-to-head: `a11y-computer-use bench h2h` runs a browser task suite (12 tasks at first, 13 with `dropdown_custom`) (`a11y_computer_use/arena_tasks/`, instrumented pages that record clicks, inputs, and a state digest) through three loops with the same planner: the reference agent loop on accessibility refs, a screenshot-only coordinate loop executed by the Anthropic computer-use adapter, and the same loop with snap-to-ref. It scores completion, planner turns, actions, misclicks (counted by the page), wasted actions, tokens, reported and estimated cost, and wall time, writes markdown and JSON reports, and serves the fixtures over local HTTP so iframes work. `ClaudeCLIProvider` gains `view_images` (the newest screenshot written to a temporary PNG, Read tool only), runs with `--strict-mcp-config`, and reports the CLI's per-call cost in `Usage.cost_usd`. `docs/benchmark.md` describes the method (6ee97b9, 2026-09-02).
- Head-to-head comparability: task manifests can flag a mode as not comparable (the native `select` popup is not painted in headless Chrome), the report shows completion rates with and without flagged tasks, `--render` re-renders saved `h2h.json` files with the current manifests, and the `dropdown_custom` task (a DOM-rendered listbox) gives the dropdown a fair three-way comparison (7078709, 2026-09-02).
- The head-to-head state digest counts focus changes, so a click that only focuses a field is no longer scored as wasted (9e27bdc, 2026-09-02).
- Both head-to-head loops receive the same approving confirmation callback, so the destructive-label gate cannot decide the comparison (a ref click on a button titled Delete trips it; a coordinate click carries no title) (7618d05, 2026-09-02).
- The live `scroll_to_find` check gives the loop enough scroll steps to reach a list item 3,200 px down (d11d407, 2026-09-02).
- `docs/benchmarks/h2h-2026-09-02.md` and `.json`: the first dated head-to-head result. One planner (`claude-fable-5-1` through the Claude Code CLI), 13 tasks, one round: refs 13/13 with 0 misclicks at $7.04 reported cost; pixels 7/13 with 27 misclicks at $11.74; pixels+snap 6/13 with 31 misclicks at $12.07; on the 12 comparable tasks 12/12 vs 7/12; pixels won `search_filter`. Superseded pre-fix rows are kept alongside (35ac0f6, 2026-09-02).

### Changed

- License changed from MIT to Apache-2.0; `NOTICE` and the PyPI license classifier added (bf2355a, 2026-07-12).
- `server.py` no longer imports pyobjc at module import, so `build_server()` runs on Windows; the Windows CI job gained a build smoke step (cbf7904, 2026-07-13).
- Simplification pass over the browser driver, arena, and Runtime: duplicate code removed and fewer CDP round-trips per operation (2bdce2d, 2026-09-01).
- One shared `observe.rematch_ref` handles ref re-resolution for the macOS, Linux, and browser drivers, and the `app`, `window`, and `clipboard` tools route through the driver. `window raise` still calls `NSRunningApplication` directly and is macOS-only code (bcce4fa, 2026-09-01).
- `a11y-computer-use snapshot` resolves its backend through `drivers.get_driver()` like `run-once` and `mcp`, so it works on every OS and honours `A11Y_COMPUTER_USE_DRIVER`; `doctor` help text names the per-platform checks (5f7766a, 2026-09-02).
- `window raise` runs through the new `Driver.window_owner` / `raise_window` seam: macOS behaviour unchanged, Linux raises by X window id via `_NET_ACTIVE_WINDOW`, browser and Windows answer a structured `unsupported` (efd9d81, 2026-09-02).
- `scroll_to_find` anchors its wheel on the largest list, table, or outline container below the window rather than the window itself; the browser backend exposes an overflow list as `AXList`, so the previous anchor scrolled the page (d6c19d9, 2026-09-02).

### Fixed
- Agent loop: planner transport failures (timeouts, connection resets, truncated bodies) end the run with `stopped=provider_error` and an audit row instead of an unhandled exception; `done(success)` is validated (string booleans flagged as `invalid_done_arguments`, anything else fails the run); a `done` issued alongside other tool calls is deferred until their results are seen (b8b297f, 2026-09-02).
- Adapters: coordinate clicks snap to a ref only for a plain left click, never against a stale snapshot when the refresh fails, and never inside a populated field or a slider; OpenAI `pending_safety_checks` block execution until acknowledged; screenshot coordinate mapping is normalised to the display size, and the browser driver captures a CSS-sized bitmap on HiDPI tabs (b8b297f, 2026-09-02).
- Observe: the snapshot depth cap now bounds the tree walk itself, so deep wrapper chains and cyclic fan-out are read in bounded time (b8b297f, 2026-09-02).
- Linux: `pids_matching` matches the window owner's comm only; AltGr-only keysyms are typed through a spare keycode; the AT-SPI focus probe asks the Collection interface first and `type` refuses when the probe cannot decide (b8b297f, 2026-09-02).

- Linux CI live AT-SPI2 step no longer exits with code 137 from a `pkill` self-match (dd79f5f, 2026-08-28).
- Headless Chrome launch in the browser CI job: added `--disable-dev-shm-usage`, a wait for `/json/version`, and diagnostics on failure. The CI run for the previous commit had failed at this step with exit code 7 (3b331ba, 2026-09-01).
- Set-of-Mark: `marks_for` (`a11y_computer_use/marks.py`) mapped element bounds through a nonexistent `ScaledImage.to_scaled`, so `screenshot(marks=true)` raised `AttributeError` on any real `ScaledImage` with an element to mark; it now maps through `ScaledImage.from_source`, and the test fake in `tests/test_marks.py` uses the real method name. Covered by that unit test with a stub only, not by a live screenshot test (40973f2, 2026-09-02).
- CLI description, package and server module docstrings, and the MCP `_INSTRUCTIONS` no longer describe a macOS-only 12-tool server; they describe the cross-platform surface without a hard-coded count. The adapter examples printed `Result.error` and `Result.text` together, which rendered `app_not_found: app_not_found: ...`; they print the text alone, with a test pinning that the code appears exactly once (b4ece8a, 2026-09-02).
- `Runtime.click(x, y)` with no `display_id` filled it from `Quartz.CGMainDisplayID()` unconditionally, so it raised `NameError` on Linux and would have on Windows (found on a real Ubuntu desktop, `docs/box-testbed.md`). The `Driver` protocol gains `main_display_id()`: macOS returns `CGMainDisplayID`, the Linux, Windows, and browser drivers return 0 (the id their `primary_geometry` stamps on snapshots and screenshots). Hermetic tests cover the seam and every backend (838165d, 2026-09-02).
- Linux coordinate input: `_linux_input.click`/`drag`/`scroll` positioned the pointer with `Display.warp_pointer(x, y)`, which X treats as a move relative to the current pointer, so clicks landed at pointer + (x, y); they now queue an absolute XTEST `MotionNotify` before the button events. `_linux_system._geometry_on_root` translated the root origin into window coordinates, so the act-time hit-test never matched a window away from the origin and every `Runtime.click` ended in `focus_changed`; it now translates the window origin into root coordinates, which also fixes window-list bounds. Both bugs were invisible to the Xvfb CI job (every window and the pointer sit at 0,0 there) and were found on a real Budgie/Xorg desktop (`docs/box-testbed.md`). Adds fake-Xlib synthetic tests (`tests/test_linux_synthetic.py`, `tests/test_linux_system_synthetic.py`), two live tests that park the pointer off-origin before a coordinate click, and `scripts/box/verify-pointer.sh` with `pointer_probe.py` (6240b56, 2026-09-02).
- Pre-gate refusals are audited: a ref that fails to resolve (`stale_ref`) and `set_value` on a secure field (`secure_field`) now write an audit row with `decision: null`, with the ref and role only and never the value (efd9d81, 2026-09-02).
- Pointer actions refuse secure fields on every driver: a resolved secure element, or a raw point inside one in the latest snapshot, for `click`, `drag` (either endpoint), and wheel `scroll`. Previously a ref click on a password field fell back to a raw pointer click on the browser and Linux backends (efd9d81, 2026-09-02).
- Browser wheel scroll direction was inverted: `drivers/browser.py` negated `dy` the way the macOS driver does, but CDP already uses the tool contract's sign, so every scroll down at the top of a list was a no-op. Found by the head-to-head `long_list` task, which all three modes failed before the fix (b7cd8db, 2026-09-02; test pinned in b31f477, 2026-09-02).
- `a11y_computer_use.act` imports on Linux and Windows (the CGEvent tables are built only when Quartz imported), so the full suite collects and passes off macOS: `tests/test_server.py` builds its Runtime on the macOS driver seam it mocks, doctor assertions are platform-aware, Linux live tests skip without a display, and `h2h.load_tasks` ignores dot-prefixed sidecar files such as AppleDouble `._*.json` (00d2ea2, 2026-09-02).
- The suite passes on Windows runners: `doctor`'s first check is `uiautomation_import` there, and an autouse conftest shim makes `Path.home()` honour `HOME` on win32 so tests that redirect `HOME` to a temp dir no longer read and write the runner's real `~/.a11y-computer-use` (0c5eba7, 2026-09-02).
- `scripts/box/bootstrap.sh` installs `dbus-x11`; without `dbus-launch` the `org.a11y.Status` flip cannot autolaunch a session bus after a box resume (9ff0167, 2026-09-02).

### Performance

- Linux: XTEST events are batched and flushed once per operation (51dbcbc, 2026-08-29).
- Linux: the value probe is skipped on roles that carry no value, saving D-Bus round-trips (3f50039, 2026-08-29).
- Linux: opt-in AT-SPI event thread (`A11Y_COMPUTER_USE_ATSPI_EVENTS=1`) that trusts libatspi's read cache. The commit message reports about 1.8x on its own measurement; that figure is not reproduced in CI (3371405, 2026-08-29).
- Linux: event-driven `wait_for` on the a11y event thread when the opt-in is set (bfe469e, 2026-08-29).
- Observe: the prune depth cap is counted over kept ancestors rather than raw wrapper nodes (`_prune_inner` carries a lower bound, `_enforce_depth` applies the exact cap afterwards, `_MAX_RAW_DEPTH` keeps the old protection against pathological trees), so deep collapsed wrapper chains no longer stop the walk one level short. Commit-reported numbers on news.ycombinator.com, same frame: `… 1 more` markers 209 -> 1, elements 330 -> 435 (actionable 79 -> 103), full view 4,511 -> 4,539 tokens, interactive 759 -> 1,089; not reproduced in CI. Existing fixtures unchanged; two synthetic tests added; `docs/observation-cost.md` corrected (a9e42d9, 2026-09-02).

### CI

- First GitHub Actions workflow: cross-platform install plus a Windows validation job on `windows-latest` (driver selection smoke, core tests) next to the macOS job (2a02599, 2026-07-13).
- The Linux job (apt AT-SPI2, GTK, and Xvfb packages; live GTK3 test under `xvfb-run` and `dbus-run-session`) arrived with 445dd60, and the live cu-arena step with 6df365e; both are listed under Added.
- Every runner runs the full hermetic suite with `pytest -q -rs`; the Linux live step runs under the openbox window manager so the real-desktop coordinate tests execute in CI; the browser job adds live adapter, agent-loop, head-to-head, and `bench desktop` steps; a `package` job builds with uv, runs the console script through uvx, and checks that the sdist excludes brand media (`[tool.hatch.build.targets.sdist]`, 4.6 MB to 454 KB); `concurrency` cancels superseded runs and the token is read-only. `docs/ci.md` describes each job (eace75c, 2026-09-02).

### Docs

- docs/decisions/plan-2026-07.md reframed after the COM-6 token measurements: the wedge is refs, not token savings (0af7c04, 2026-07-12).
- Launch-ready README with the a11y-first positioning (6a34ee3, 2026-07-12).
- COM-3: the name `a11y-computer-use` is kept (445f0b2, 2026-07-12).
- COM-11: language boundary decision, stay Python for Phase 1 and defer Rust/Swift; `docs/decisions/language-boundary.md` (c86cdb4, 2026-07-13).
- COM-12: Phase 0 go/no-go review with verdict GO to Phase 1; `docs/decisions/phase-0-review.md` (d07630e, 2026-07-13).
- README and PLAN reframed around embedding into AI-platform products, with the host app owning signing and TCC grants (23a7856, 2026-07-13).
- `examples/`: in-process Python embed, Node MCP-subprocess embed, and a README (6e97131, 2026-07-13).
- Hero demo GIF (`docs/hero-demo.gif`) and a measured-results section in the README (bad6136, 2026-07-13).
- `docs/linux-port.md`: Wayland support matrix (a11y and capture native; coordinate input gated) (c7ebfa7, 2026-08-29).
- `a11y_computer_use/drivers/linux.py`: the `_grab_wayland` docstring now says the grim capture path was exercised manually under headless sway (2026-08-29) and has no automated test, instead of "verified live" (42491a7, 2026-09-02).
- README rewritten from an evidence-cited fact check of the code (four drivers, the 16+2 tool surface, a platform matrix with the exact gates, measured numbers with provenance, embedding shapes, safety model, architecture), plus CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, CITATION.cff, issue forms, a PR template, dependabot, CODEOWNERS, `.editorconfig`, a docs index, brand assets under `docs/assets/`, pyproject metadata, and corrections to stale statements in the existing docs and CI comments (b59cc09, 2026-09-02).
- `docs/box-testbed.md`: the real-desktop Linux run, the four bugs Xvfb hid, the whole-suite desktop run, `doctor` 8 of 8, and the keyboard round-trip; `scripts/box/README.md` syncs with `COPYFILE_DISABLE=1` so macOS tar ships no AppleDouble sidecars (fada77e, 2f9fa73, 8e431e3, 2026-09-02).

Dates are author dates as printed by `git log --date=short` (for one rebased commit, 445dd60, the committer date is 2026-08-25), not release dates. v0.1.0 is the first tag and was not published on PyPI; releases from 0.1.1 are.

[Unreleased]: https://github.com/Perception-Dynamics-Inc/a11y-computer-use/compare/v0.2.1...main
[0.2.1]: https://github.com/Perception-Dynamics-Inc/a11y-computer-use/releases/tag/v0.2.1
[0.2.0]: https://github.com/Perception-Dynamics-Inc/a11y-computer-use/releases/tag/v0.2.0
[0.1.1]: https://github.com/Perception-Dynamics-Inc/a11y-computer-use/releases/tag/v0.1.1
[0.1.0]: https://github.com/Perception-Dynamics-Inc/a11y-computer-use/releases/tag/v0.1.0
