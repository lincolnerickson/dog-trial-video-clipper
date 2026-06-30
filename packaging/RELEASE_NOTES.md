Self-contained macOS app for cutting one long 4K dog-trial recording into one named clip per run. Nothing to install — Python, the GUI, and ffmpeg are bundled.

## Download

- **Apple Silicon (M1/M2/M3/M4) — `DogTrialVideoClipper-macOS-arm64.zip`** ← use this for an M2 Pro
- **Intel Macs — `DogTrialVideoClipper-macOS-x86_64.zip`**

Not sure which?  menu → **About This Mac**: "Apple chip" → arm64; "Intel" → x86_64.

## First launch (do this once)

The app isn't notarized, so macOS blocks it the first time:

1. Unzip, drag **Dog Trial Video Clipper.app** into **Applications**.
2. **Right-click the app → Open → Open** (double-clicking the first time only shows a warning). On newer macOS, instead go to  → **System Settings → Privacy & Security**, scroll down, and click **Open Anyway**.
3. After that, it opens normally.

If macOS says it's *"damaged"* and won't offer Open, run this in **Terminal**, then open it again:

```
xattr -dr com.apple.quarantine "/Applications/Dog Trial Video Clipper.app"
```
