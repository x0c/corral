# Public presentation

## Product identity

The approved Corral icon is the colorful paper-cut unicorn on a blue background. Use the current iOS master at `../../ios/Corral/Assets.xcassets/AppIcon.appiconset/AppIcon.png` in the product workspace. The green arrow and the orange/sage horse artwork are retired; remove their files and generated packages rather than retaining competing masters. Do not replace the unicorn with a newly generated interpretation.

The README display asset is `docs/screenshots/corral-unicorn.png`: a compact rounded clip of the approved master. Keep the application master square and opaque. Do not embed the 1024 master in an SVG for GitHub.

## README contract

Keep English and Simplified Chinese READMEs aligned. Lead with the purpose, a real product demonstration, and installation. Explain finding sessions, keeping agents running, working side by side, and handing work to another assistant. Keep exhaustive command lists, internal architecture, implementation rules, and historical fixes in the linked guides. Do not add keyword dumps, speculative claims, unavailable download links, or repeated requests for stars. State iPhone availability and self-hosted relay requirements honestly.

Use current, sanitized product captures. The README hero demonstration is `docs/screenshots/demo.gif`, generated from the real terminal UI by `docs/screenshots/capture.py` with isolated sample conversations. A historical animated capture such as `demo-list.gif` must not return; it showed command output and local paths. Static `list.png` remains the still; fold `search.png`. Phone README shots are the session list and conversation, not the machine picker. Check both rendered languages, images and links after changes.

## Structural references

- [Sesh](https://github.com/joshmedeski/sesh): concise identity and task-oriented feature summaries; adopt these patterns, not its configuration-heavy page length.
- [Lazygit](https://github.com/jesseduffield/lazygit): show the actual terminal product early and link deeper usage; do not copy sponsor blocks or badge volume.

README quality improves the explanation offered to visitors; it does not demonstrate increased discovery or stars. Measure those separately.
