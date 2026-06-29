# Packaging the macOS app

This builds a **self-contained `Dog Trial Video Clipper.app`** — Python, PySide6
(the GUI + video player), and ffmpeg are all bundled inside. The person running
it needs **nothing installed**: they double-click the app.

Because a Mac app can only be built on a Mac, the build runs on **GitHub's macOS
runners** (no Mac needed locally). It produces two apps — one for **Apple
Silicon** (`arm64`) and one for **Intel** (`x86_64`) — so you can send whichever
matches the videographer's Mac.

---

## How to build

1. Push this repo to GitHub (one-time) and make sure the workflow file
   `.github/workflows/build-macos.yml` is on the default branch.
2. Start a build, either way:
   - **By hand:** GitHub → the repo → **Actions** tab → **Build macOS app** →
     **Run workflow**.
   - **By tag:** `git tag v1.0.0 && git push origin v1.0.0`.
   - **From the terminal:** `gh workflow run "Build macOS app"`.
3. When it finishes (~5–10 min), open the run and download the **Artifacts**:
   - `DogTrialVideoClipper-macOS-arm64` — for Apple Silicon Macs (M1/M2/M3/M4).
   - `DogTrialVideoClipper-macOS-x86_64` — for Intel Macs.
   Each is a `.zip` containing the `.app`.

To check progress from the terminal: `gh run watch` (or `gh run list`).

### Which one does the videographer have?

 Apple menu →  **About This Mac**. If it says **Apple M1/M2/M3/M4** (or just
"Apple chip"), send the **arm64** zip. If it says **Intel**, send the **x86_64**
zip. When unsure, the **x86_64** build also runs on Apple Silicon (via Apple's
Rosetta, which installs itself on first launch) — but the native arm64 build
scrubs 4K more smoothly, so prefer the matching one.

---

## First launch on the videographer's Mac (important)

The app is **not code-signed** (that needs a paid Apple Developer account), so
macOS Gatekeeper will block it the *first* time with a "can't be opened" or
"damaged" message. This is expected. Two ways past it — do this **once**:

- **Easiest:** unzip, drag **Dog Trial Video Clipper.app** to **Applications**,
  then **right-click (or Control-click) the app → Open → Open**. After that first
  time it opens normally by double-click.
- **If macOS says it's "damaged" and won't even offer Open** (the quarantine
  flag from downloading), open **Terminal** and run:

  ```bash
  xattr -dr com.apple.quarantine "/Applications/Dog Trial Video Clipper.app"
  ```

  then double-click it.

Send the videographer the relevant line above along with the app.

> Want to skip this entirely? Signing + notarizing with an Apple Developer
> account ($99/yr) makes the app open with a normal double-click and no warning.
> The workflow has `codesign_identity`/`entitlements` hooks ready for that as a
> later upgrade.

---

## What's inside / how it works

- **Entry point:** `marker.py` (the GUI). It pulls in `clipper/`, `markerlib/`,
  and `cutter.py` automatically.
- **ffmpeg:** the `imageio-ffmpeg` binary is bundled. On launch the frozen app
  points `IMAGEIO_FFMPEG_EXE` at it and marks it executable
  (`marker._prepare_bundled_ffmpeg`). A real system ffmpeg on `PATH` is still
  preferred if present (same order as everywhere else).
- **Self-test:** the workflow runs `… --selftest <clip>` headless after building
  to confirm the bundle launches and all Qt/PySide6 plugins are present. It's
  marked non-blocking (a headless runner can't fully exercise video decode), so
  read its log if the app misbehaves on a real Mac.
- **Spec:** `packaging/Dog_Trial_Video_Clipper.spec` (PyInstaller). To add an
  icon, drop `packaging/icon.icns` and rebuild — the spec uses it automatically.

## Building locally on a Mac (optional)

If you ever do have a Mac:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm packaging/Dog_Trial_Video_Clipper.spec
open dist   # the .app is here
```
