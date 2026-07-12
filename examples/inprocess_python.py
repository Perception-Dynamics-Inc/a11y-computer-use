"""In-process embed (Python host): import computerUse and drive the desktop
directly, under YOUR app's identity and permission grants.

This is the cleanest integration for a Python AI platform — computerUse runs
*as* your process, so its code-signing identity, entitlements, and TCC grants
are simply your app's. You never ship a computerUse binary or certificate.

Run (needs Accessibility granted to whatever launches this):

    python examples/inprocess_python.py

It takes a read-only accessibility snapshot of Finder (always running), then
shows the shape of the act calls. Nothing is clicked or typed.
"""

from computeruse import safety, server

APP = "com.apple.finder"  # always running; a READ-tier snapshot is side-effect-free


def main() -> None:
    # YOUR app owns the permission store and the audit log. Grant per app, per
    # tier: READ = observe/screenshot; CLICK = pointer; FULL = typing/keys.
    store = safety.PermissionStore()  # defaults to ~/.computeruse/permissions.json
    store.set_tier(APP, safety.Tier.READ)

    runtime = server.Runtime(store=store)

    # observe: a pruned accessibility tree with element refs (e1, e2, ...),
    # no pixels. Every ref is re-resolved against the live tree at act time.
    tree = runtime.desktop_snapshot(APP, scope="window")
    print(tree)

    interactive = tree.count("(click") + tree.count("(edit")
    print(f"\n# {interactive} actionable refs found via the accessibility tree.")
    print("# To act, grant a higher tier and target a ref from the tree above:")
    print("#     store.set_tier(APP, safety.Tier.FULL)")
    print("#     runtime.click(ref='e14')            # activates via AX — no cursor movement")
    print("#     runtime.type_text('hello world')    # into the focused element")
    print("#     runtime.scroll(ref='e20', into_view=True)   # reveal, cursor-free")
    print("#")
    print("# If a snapshot reports 'no interactive elements', the app is custom-drawn")
    print("# (e.g. Telegram) — fall back to runtime.screenshot() + coordinate clicks.")


if __name__ == "__main__":
    main()
