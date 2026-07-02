Self-contained macOS app for cutting one long 4K dog-trial recording into one named clip per run. Nothing to install — Python, the GUI, and ffmpeg are bundled.

## What's new in 1.0.17

- **Fixed: crash report on quit while a job was encoding.** Quitting during an export/join now stops ffmpeg immediately and exits cleanly; the job still resumes on next launch.
- **Fixed: interrupted or cancelled jobs could leave a broken video file** (one that won't open) in the output folder. Failed and cancelled outputs are now cleaned up automatically.
- **Fixed: Cancel could take minutes** to actually stop a long encode; it now stops within seconds.
- **Fixed: editing a clip, then deleting or reordering another row, could save the edit onto the wrong clip.**
- Reliability fixes around quitting mid-batch: jobs no longer restart during shutdown, and cancelled jobs no longer come back after a crash.

## Download

- **Apple Silicon (M1/M2/M3/M4) — `DogTrialVideoClipper-macOS-arm64.zip`** ← use this for an M2 Pro

Requires an Apple Silicon Mac ( menu → **About This Mac** shows an "Apple" chip). There is no Intel build — GitHub retired its free Intel macOS runners.

## First launch (do this once)

The app isn't notarized, so macOS blocks it the first time:

1. Unzip, drag **Dog Trial Video Clipper.app** into **Applications**.
2. **Right-click the app → Open → Open** (double-clicking the first time only shows a warning). On newer macOS, instead go to  → **System Settings → Privacy & Security**, scroll down, and click **Open Anyway**.
3. After that, it opens normally.

If macOS says it's *"damaged"* and won't offer Open, run this in **Terminal**, then open it again:

```
xattr -dr com.apple.quarantine "/Applications/Dog Trial Video Clipper.app"
```
