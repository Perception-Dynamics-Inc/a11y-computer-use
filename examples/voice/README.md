# Voice demo

The six spoken commands from the "instant computer use" demo, run through
the reflex layer (`docs/voice.md`): Notes, a new note titled Hello, the
browser, a Google search, x.com, and a Photo Booth picture.

```
a11y-computer-use voice --grant full --apps "Notes,Google Chrome,Photo Booth" \
    --text examples/voice/demo-transcript.txt --router local
```

`demo-transcript.txt` keeps the fillers a real transcript carries ("um",
"once you're there", "can you", "Nice, nice."); the splitter drops them and
cuts each line into commands. "Arc browser" opens Chrome on a Mac without
Arc.

## Measured timeline

Local router, `--text` (so no speech stage), macOS 26 on Apple silicon:

```
[route 0 ms local] [act 62 ms] open_app app='Notes' -> ok: focused com.apple.Notes (Notes frontmost)
[route 0 ms local] [act 94 ms] new_document  -> ok: pressed cmd+n
[route 0 ms local] [act 153 ms] set_title text='Hello' -> ok: typed 5 characters
[route 0 ms local] [act 150 ms] open_app app='Google Chrome' -> ok: focused com.google.Chrome (Google Chrome frontmost)
[route 2 ms local] [act 350 ms] web_search query='Norbert Wiener' -> ok: opened https://www.google.com/search?q=Norbert+Wiener in a new Google Chrome tab (Google Chrome frontmost)
[route 0 ms local] [act 141 ms] open_url url='x.com' -> ok: opened https://x.com in a new Google Chrome tab (Google Chrome frontmost)
[route 0 ms local] [act 42 ms] open_app app='Photo Booth' -> ok: focused com.apple.PhotoBooth (Photo Booth frontmost)
[route 0 ms local] [act 238 ms] take_photo  -> ok: pressed cmd+return
8/8 commands ok
```

Jev router (`--router jev`, key from `TYPESAFE_API_KEY`), same commands:

```
[route 1017 ms jev] [act 204 ms] open_app app='Notes' -> ok: focused com.apple.Notes (Notes frontmost)
[route 953 ms jev] [act 124 ms] new_document  -> ok: pressed cmd+n
[route 815 ms jev] [act 43 ms] set_title text='Hello' -> ok: typed 5 characters
[route 1699 ms jev] [act 163 ms] open_app app='Google Chrome' -> ok: focused com.google.Chrome (Google Chrome frontmost)
[route 739 ms jev] [act 240 ms] web_search query='Norbert Wiener' -> ok: opened https://www.google.com/search?q=Norbert+Wiener in a new Google Chrome tab (Google Chrome frontmost)
[route 1963 ms jev] [act 195 ms] open_url url='x.com' -> ok: opened https://x.com in a new Google Chrome tab (Google Chrome frontmost)
[route 3018 ms local-fallback] [act 808 ms] open_app app='Photo Booth' -> ok: launched Photo Booth; first window: 'Photo Booth' (Photo Booth frontmost)
[route 2256 ms jev] [act 205 ms] take_photo  -> ok: pressed menu item 'Take Photo' in com.apple.PhotoBooth
8/8 commands ok
```

Both routers picked the same skill for every command. Jev answered in 0.7 to
2.4 s per call from this network and one call timed out (2 s) and fell back
to the local router, which is why `local` is the default. Photo Booth was
not running before the Jev run, so its first command is a launch (808 ms,
waiting for the window) rather than a focus.

Speech endpointing is missing from both timelines: the Speech Recognition and
Microphone grants for this terminal are not determined, and granting them is
the owner's call. Run without `--text` once to get the system prompts, then
the `[stt N ms]` field appears on the first command of each utterance.
