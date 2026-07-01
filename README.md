# Dog Trial Video Clipper

Cut one long, continuous 4K trial recording into one named clip per participant
run — replacing the slow, one-at-a-time CapCut export workflow.

There are two parts:

1. **Marker** (`marker.py`) — a native desktop app to scrub the video and mark
   one clip per run by clicking a participant from a loaded roster for each
   In/Out. Exports a clip-list **CSV** and can cut the clips directly.
2. **Cutter** (`cutter.py`) — a script that reads the CSV + source video and
   writes one named clip per row using an ffmpeg **stream copy** (no re-encode),
   so a whole trial's worth of 4K clips finishes in seconds.

Both share one module (`clipper/`) for timecodes and filename sanitizing, so a
label always maps to the same filename on both sides.

---

## Setup

### Windows

A virtual environment is already created in `.venv` with everything installed.
If you ever need to recreate it:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### macOS

> **Just want to hand someone a ready-to-run app?** A self-contained
> **`Dog Trial Video Clipper.app`** (Python + ffmpeg bundled, nothing to install)
> can be built in the cloud — no Mac required on your end. See
> [`PACKAGING.md`](PACKAGING.md). The source-based setup below is for developing
> or running from a checkout.

The app runs on macOS too (Intel and Apple Silicon). The Windows `.venv` won't
work there, so build a fresh one — once — by running the setup script. In
Terminal, from the project folder:

```bash
bash setup_mac.command
```

That creates `.venv` and installs the dependencies. After it finishes,
`run_marker.command`, `cut.command`, and `setup_mac.command` become
double-clickable in Finder. (If you only have `python` and not `python3`, run
`PYTHON=python bash setup_mac.command`. If macOS blocks a downloaded `.command`,
right-click it → **Open** the first time.)

**ffmpeg:** required for cutting. It works out of the box on both platforms —
`imageio-ffmpeg` ships an ffmpeg binary inside the venv, so nothing needs to be
installed system-wide. For best codec coverage on unusual camera formats you can
optionally install a full ffmpeg and it will be preferred automatically
(`winget install Gyan.FFmpeg` on Windows, `brew install ffmpeg` on macOS).

The cutter looks for ffmpeg in this order: `CLIPPER_FFMPEG` env var → `ffmpeg`
on PATH → the bundled `imageio-ffmpeg` binary.

---

## Marker — marking runs

Launch it:

- **Windows:** double-click **`run_marker.bat`** (or drag a video file onto it),
  or `\.venv\Scripts\python marker.py "path\to\trial.mp4"`. If the window won't
  open, run **`run_marker_debug.bat`** to see the error message.
- **macOS:** double-click **`run_marker.command`** (after the one-time setup
  above), or `.venv/bin/python marker.py "/path/to/trial.mp4"`. Any error stays
  visible in the Terminal window it opens.

### Workflow

1. **Open video…** the trial recording. *(Or **Join videos…** if the camera
   split it into chapters — see below.)*
2. **Load roster…** a participants CSV (handler + dog columns). The names appear
   as a clickable list in the **Participants** panel.
3. Type the **Search / event label** once (e.g. `NW3_Interior`). It's appended
   to every exported filename as `Participant - Search`.
4. For each run:
   - Scrub to the start → press **↑** (set In). *(Tap `←`/`→` to nudge; **hold**
     them to scrub faster — the longer you hold, the faster it goes.)*
   - Assign the participant: **press Enter** to drop the next name from the roster
     on (top of the Participants list), or **click** their name in the list.
   - Scrub to the end → press **↓** (set Out).
   - The clip is **added automatically** and that participant **leaves the
     roster** — so you always know who's left to film.
5. Reorder, edit, or delete rows any time before export.
6. **Export CSV…** to save the clip list, and/or **Export clips…** to cut the
   clips right now. Export asks whether to **group into a folder per participant**
   (handler + dog) — see below. Cutting runs **in the background** in a **queue**:
   each **Export clips…** adds a job (with its own source video + clip list), and
   they cut one after another while you work. Hitting Export also **clears the
   window** (clips, marks, search; the roster is restored) so you can immediately
   **open and mark the next camera view.** A progress bar under the clip list shows
   the current job and how many are queued, with a Cancel button; a summary
   appears when the whole queue finishes.

You can also type a name directly into the *Participant* field for anyone not in
the roster (ad-hoc entries don't consume a roster name).

### Reusing the running order across camera views

A trial is often filmed from several cameras (interior, exterior, …). The
participants run in the **same order** in every view — but in the *first* view
you don't yet know that order, you discover it as you mark. Once the first view
is done, you shouldn't have to figure out who's who all over again for views 2–5.

So the running order can be saved from the first view and reused:

1. **First view** — mark as usual (click each participant as you identify them).
   The order of your clip list, top to bottom, *is* the running order.
2. Click **Save running order…** (in the Participants panel) to write that order
   to a small file next to the video (`running_order.txt`). *(It's also kept in
   memory for the rest of the session, so you can jump straight to the next view
   without reloading.)*
3. **Next views** — open the next camera's video, load the same roster, then
   click **Use saved order…** and pick the file. The Participants list **reorders
   into running order**, so the **top name is always the next run**.
4. Now marking is pure keyboard: **↑** (In) → **↓** (Out) → **Enter** drops the
   top name onto the clip, it auto-adds, and the next name rises to the top. The
   Participant field shows who's next (`press Enter → Sara & Tracer`).

If a run happens out of order (a dog scratches, or runs early), just **click the
correct name** instead of pressing Enter — that always wins, and the order picks
up from there. Names in the file that aren't in the roster are ignored; roster
names not in the file simply sort to the bottom.

### Output folders (one per participant) & filenames

When you **Export clips…**, choose **Yes** to "group into a folder per
participant" and each clip lands in a folder named **`First & Dog`** (the
handler's first name and the dog), with the file named for the **search/event
label** — because the folder already says who it is:

```
Sara & Tracer/
   Interior Search 1.mp4
   Exterior Search.mp4
Sara & Otter/
   Interior Search 1.mp4
```

- The participant (e.g. `Sara & Tracer`) is the folder; the same handler with a
  different dog (`Sara & Otter`) gets its own folder. The **filename is the
  search/event label** you typed (`Interior Search 1.mp4`).
- Exporting a **later** video's clips into the **same** output folder **reuses**
  those folders — clips accumulate, no `Sara & Tracer (2)/` duplicates.
- A new clip **never overwrites** an existing file: if a file of the same name is
  already there (same participant + same search label), the new one becomes
  `… (2).mp4`, so nothing is lost.
- Choose **No** for a single flat folder instead — there the filename keeps the
  participant, `First & Dog - Search.mp4`, so the files stay unique and grouped.

### Reopening a clip list to fix a clip

Every **Export clips…** also drops a small **clip-list CSV** in the output folder
(named for the search label, e.g. `Interior Search 1 clips.csv`) — a record of
exactly where each clip is. If a clip needs fixing later, you don't have to scrub
the whole trial to find it:

1. Open the source video (or the joined file) and click **Load clip CSV…**, then
   pick that saved file. The clips reappear, with the participant back in the
   *Participant* field and the search label back in the **Search/event** box.
2. **Double-click** the clip to fix — it jumps straight to that spot — nudge
   In/Out (scrub + `↑`/`↓`), then **Update**.
3. **Export clips…** again. It's a fast stream copy, so re-cutting is seconds.

(The manual **Export CSV…** writes the same file wherever you choose, if you want
an extra copy.)

### Intro & outro cards on every clip

Want a branded title card at the **start** of every clip, and/or a card at the
**end** (e.g. a bullseye map of where the hides were)? Next to **Intro card** /
**Outro card**, click **Choose image…**, pick a still (PNG/JPG/…), and set
**Show for** to how many seconds it should hold (default 3). On **Export
clips…** the intro is placed at the start and the outro at the end of every
exported clip. **Clear** removes either.

- The run footage is **still a lossless stream copy** — only the short cards are
  encoded, so export stays fast even at 4K. (Each card is encoded once and
  reused for every clip.)
- Each image is **letterboxed** onto the video's frame, so any aspect ratio is
  fine — scaled to fit and centred on black, never stretched. For an outro
  marking locations, just place the bullseyes in your image; the same image
  (same positions, same duration) is applied to every clip.
- A silent audio track matching the video is added under each card, so the
  delivered clip has clean audio throughout.
- Works for **H.264 and H.265** sources (what trial cameras produce).

> **Why a still card at the end, not an overlay on the footage?** A graphic
> burned over the *moving* run can be confusing (is that bullseye on the dog?).
> A static end card shows the markers clearly with nothing moving. If you ever
> want the markers over the live footage instead, that's a different (re-encode)
> mode we can add.

### Export video format (size vs quality)

The **Export video** dropdown picks how each clip's video is written:

- **Smaller — HEVC (recommended, default):** re-encode to H.265 at a target
  **Bitrate** (default **12 Mbps**) — for high-bitrate GoPro 1080p60 source
  (~30–60 Mbps), ~12–15 Mbps looks clean and is far smaller than the source,
  keeping full detail for zooming in, and it plays natively on modern devices
  (iPhones, Macs, Windows 11). It uses your **Mac's hardware encoder**
  (VideoToolbox), so it's **fast** — the same engine CapCut uses. Dial the bitrate
  down to find the smallest that still looks good (too low gets grainy).
- **Original — no re-encode:** a **lossless stream copy** in the source's own
  codec — largest files, but exports in **seconds**. Best when you want the exact
  original, or a server/host re-encodes for you.
- **Web-safe — H.264:** for clips that must play **directly in Chrome/Firefox**.
  H.265 sources are re-encoded to H.264 (GPU if available), H.264 sources are
  stream-copied; always **fast-start** so a browser previews instantly. (Not
  needed if a server makes the streaming version, or buyers are on Apple/modern
  devices.)

> **Why HEVC for "smaller"?** A re-encode can't add detail beyond the source, so
> the only way to beat the original's size *without* throwing away detail is a
> more efficient codec — and H.265 is ~2× more efficient than H.264. CRF (quality)
> than H.264 — so a clip stays small without throwing away detail. The marker
> drives the **hardware** HEVC encoder by a **target bitrate** (portable across
> encoders, unlike a CRF quality number), which is what makes it fast. (The CLI
> can also do a `libx265` CRF instead, via `--video-mode hevc --crf N` — best
> compression, but software-slow.)

### Joining GoPro / DJI chapters into one file

A GoPro automatically splits one long recording into ~1-hour **chapters**
(`GX010078.MP4`, `GX020078.MP4`, … — same trailing number, increasing chapter
number); DJI cameras do the same. To mark across the whole trial as one timeline:

1. Click **Join videos…** and select all the chapters of the recording (you can
   select them all at once — they're put in chapter order automatically).
2. Confirm the order. Leave **Encode to delivery quality now** ticked
   (recommended — see below), then choose where to save the file.
3. The job is added to a **background queue** and does **not** auto-load. Do this
   for each camera view / recording, then **let the queue run overnight** — jobs
   process one at a time (and never fight an export for the machine). A progress
   bar shows the current job and how many are queued; **Cancel** stops the running
   job and drops the rest of the queue.
4. Next day, **Open video…** an encoded file and mark as usual. Because it's
   already at delivery quality, **exporting clips is an instant stream copy** —
   a fraction of a second per clip instead of a re-encode.

**Encode now, clip instantly later.** With the encode box ticked, the whole
recording is re-encoded **once** to delivery-quality HEVC at your bitrate (the
**Smaller — HEVC** setting, default ~30 Mbps) with frequent keyframes. That's the
only encode the footage ever gets — clips then copy straight out of it with no
quality loss and no waiting. Untick the box for a **fast lossless join** instead
(original codec, no re-encode) when you just want to stitch chapters; either way
the join is queued and trailing black frames are trimmed.

The encoded file needs room on disk (a ~30 Mbps HEVC recording is usually
*smaller* than the raw high-bitrate chapters). It works for the older
`GOPR0078.MP4` / `GP010078.MP4` naming too, and for any set of clips with matching
codec/resolution/frame-rate.

**Trimming trailing black (DJI):** some cameras — notably DJI — end every
auto-split file with a few **black frames**. Joined as-is, those become a brief
**black flash at each hour boundary** in the continuous video. The app **always**
scans the tail of each file, finds exactly where the black starts (ffmpeg's
`blackdetect`), and stops reading that file there — so the black is dropped with
no real footage lost. It adds only a quick scan of each file's last few seconds,
and is harmless for cameras that don't do this (a clean file is left alone).

### Hotkeys

| Key | Action |
|-----|--------|
| `Space` | Play / pause |
| `←` / `→` | Scrub back / forward — tap to nudge, **hold to accelerate** |
| `Shift+←` / `Shift+→` | Jump 10 seconds (fixed) |
| `,` / `.` | Step one frame back / forward |
| `Home` / `End` | Jump to start / end |
| `J` / `K` / `L` | Slower / pause / faster (fast-scrub: tap L to ramp 1×→2×→4×→8×) |
| `↑` / `↓` | Set In / Out at the playhead (`I` / `O` also work) |
| `Enter` | Add the clip — or, with a roster loaded, drop the next participant on (top of the list; then it auto-adds) |
| `Ctrl+Z` / `⌘Z` | Undo the last action |

The clip auto-adds the moment In, a participant, and Out are all set (in any
order). Typing in a field is normal text — the single-key hotkeys only fire when
no text field is focused. After a clip is added, focus returns to the video so
the hotkeys work again. Speed is also selectable from the dropdown.

### Editing the list

- **Double-click** a row (or select it and click **Edit**) to load it back into
  the In/Out/participant controls. Focus goes to the **video**, so you can scrub
  and re-mark with the keyboard right away (`←`/`→`, `↑`/`↓`), then press **Enter**
  (or click **Update**) to save. To rename, click the name field or a roster name.
- **Delete** removes the selected row — and if that clip came from the roster,
  the participant is **returned to the roster**. **↑ / ↓** reorder the list;
  **Clear all** empties it (and restores all roster participants). **Restore
  all** re-fills the roster from the loaded file (names still used by clips stay
  consumed).
- **Undo** (button, or **Ctrl+Z** / **⌘Z**) reverses your **last action** — a set
  In/Out, an assigned name, an added or deleted clip, a reorder, a clear, a
  loaded CSV/roster. Each press steps back one more action (up to 100), restoring
  the clips, the In/Out marks, and the roster together.
- **Validate** checks the list and reports problems (see below).

### Validation

Run before export (and automatically at export time):

- **Errors** (block export, or skip just that row when cutting): End not after
  Start; blank label.
- **Warnings** (surfaced for review, don't block): ranges that overlap a
  neighbour — flagged "heavily" when the overlap is large.

Invalid characters in labels are fine to type — they're sanitized only when the
filename is produced.

---

## Cutter — batch cutting from the command line

```powershell
.\cut.bat --video trial.mp4 --csv clips.csv --out clips
```

Options:

| Flag | Default | Meaning |
|------|---------|---------|
| `--video, -i` | (required) | source video file |
| `--csv, -c` | `clips.csv` | clip-list CSV |
| `--out, -o` | `clips` | output folder |
| `--folder-per-participant` | off | group clips into a subfolder per participant (`First & Dog`, e.g. `Sara & Tracer/`), with the file named for just the search part (alias: `--folder-per-label`; default: one flat folder) |
| `--ext` | `mp4` | output container/extension |
| `--ffmpeg` | auto | explicit ffmpeg path |
| `--video-mode` | `copy` | `copy` = stream copy (original codec); `hevc` = re-encode smaller HEVC at `--crf` (libx265, ~half size, full detail); `h264` = browser-playable H.264 (GPU if any, fast-start) |
| `--web-safe` | off | alias for `--video-mode h264` |
| `--encoder` | auto | encoder for re-encoded rows — `exact` rows, and an explicit GPU encoder for `--video-mode hevc/h264` (e.g. `hevc_nvenc`); default `libx264`/`libx265`, H.264 auto-detected for `h264` |
| `--crf` / `--preset` | `23` / `medium` | quality/speed for re-encoded rows (lower CRF = bigger/better) |
| `--intro-image` | none | image to prepend as an intro card to every clip (H.264/H.265 sources; body still stream-copied) |
| `--intro-seconds` | `3` | how long the intro card is shown |
| `--outro-image` | none | image to append as an outro card to every clip (e.g. marking where the hides were) |
| `--outro-seconds` | `3` | how long the outro card is shown |
| `--dry-run` | off | print what would be cut (and the ffmpeg commands) without writing |

It prints a summary: how many clips were written and any rows skipped/failed and
why. Exit code is `0` on a clean run, `1` if any row was skipped or failed.

Output filenames are the readable `<label>.<ext>`, keeping spaces and
capitalization (`Sara - Interior Search 1.mp4`). If two clips would end up with
the same name, the later one gets a ` (2)` suffix so nothing is overwritten.

---

## CSV files

### Participant roster (input to the marker)

```csv
handler,dog
Smith,Rex
Jones,Bella
O'Brien,Max
```

Loaded via **Load roster…**. Handler + dog columns are joined per row into
**`First & Dog`** — the handler's first name and the dog (`Smith,Rex` →
`Smith & Rex`; `Sara Johnson,Tracer` → `Sara & Tracer`). A single
`name`/`participant` column also works, with or without a header; id-like
columns (`bib`, `number`, …) are ignored. See `sample/roster.csv`.

### Clip list (marker ⇄ cutter)

```csv
start,end,label
00:14:32.000,00:17:05.000,Smith & Rex - NW3 Interior
00:19:48.000,00:22:30.000,Jones & Bella - NW3 Interior
00:25:10.000,00:25:40.000,Photo finish - NW3 Interior,1
```

- `start` / `end` are `HH:MM:SS.mmm` (also accepts `MM:SS`, `MM:SS.s`, or plain
  seconds when hand-editing).
- `label` is what the filename is built from. When the marker exports, it has
  already folded in the search/event label as `Participant - Search`.
- Optional 4th column **`exact`** — set to `1` (or `true`/`yes`/`x`) to cut that
  one row frame-accurately by re-encoding instead of stream-copying. The marker
  writes this column only when at least one clip uses it.

Output filenames are readable, spaces and capitalization preserved. In a flat
export the file is the whole `<label>.<ext>` (`Smith & Rex - NW3 Interior.mp4`);
with **folder-per-participant** the participant becomes the folder and the file
is just the search part (`Smith & Rex/NW3 Interior.mp4`). If two clips would
collide, the later one gets a ` (2)` suffix so nothing is lost.

---

## ⚠️ Stream-copy keyframe caveat

The default cut is a **stream copy** (`-c copy`): no re-encode, so it's fast, but
it can only start on a **keyframe**. The clip therefore begins at the nearest
keyframe **at or before** the In point — so a clip may start up to ~1 second
early. For run footage this is fine, even desirable (a little handle at the head).
The Out point is honored as marked.

If a specific clip must start **exactly** on the mark, set its **`exact`** flag
(the *exact cut* checkbox in the marker, or `exact=1` in the CSV). That one row
is re-encoded (`--encoder`, default `libx264`) for a frame-accurate start; only
that clip is re-encoded, so it's still quick. On an NVIDIA GPU, pass
`--encoder h264_nvenc` to make exact rows much faster.

---

## Project layout

```
marker.py              Marking tool (GUI entry point)
cutter.py              Batch cutter (CLI + run_batch() used by the marker)
clipper/               Shared core (imported by BOTH sides)
  timecode.py            parse/format HH:MM:SS.mmm
  naming.py              filename sanitizing (label -> safe, readable filename)
  clips.py               Clip model, CSV read/write, validation
  ffmpeg_tools.py        locate ffmpeg, build/run a cut, join chapters (concat)
markerlib/               GUI-only helpers (not imported by the cutter)
  player.py              swappable video-player backend (decodes via Qt, paints frames itself)
  roster.py              parse a participant roster CSV (handler+dog -> labels)
  widgets.py             clickable roster list (click a name to assign it)
tests/
  test_core.py           shared-core + roster unit tests  (python tests/test_core.py)
  smoke_marker.py        headless workflow smoke test  (offscreen)
  test_join.py           stream-copy chapter-join test  (python tests/test_join.py)
  test_join_encode.py    encode-during-join -> HEVC, then instant clip copy  (python tests/test_join_encode.py)
  test_black_trim.py     trailing-black detect + trim at join (DJI)  (python tests/test_black_trim.py)
  test_cards.py          intro/outro card test (seams decode clean)  (python tests/test_cards.py)
  test_websafe.py        web-safe H.264 + fast-start delivery test  (python tests/test_websafe.py)
  test_hotkeys.py        ↑/↓ + I/O marking hotkeys fire correctly  (offscreen)
  test_run_order.py      running order saved from view 1 drives Enter in later views  (offscreen)
  test_undo.py           undo reverses the last action (one gesture = one step)  (offscreen)
  test_scrub.py          scrub seeks coalesce to the latest target (smooth scrubbing)  (offscreen)
  diagnose_video.py      report a file's codec/frames if video won't show
  render_check.py        confirm frames are painted to the canvas
sample/                  generated 4K test video + sample roster/clip CSVs (safe to delete)
run_marker.bat           launch the marker (Windows)
run_marker_debug.bat     launch with a console for troubleshooting (Windows)
cut.bat                  wrapper for the cutter (Windows)
setup_mac.command        one-time venv setup (macOS)
run_marker.command       launch the marker (macOS)
cut.command              wrapper for the cutter (macOS)
```

### Note on video rendering & 4K scrubbing

Smooth 4K scrubbing is the main technical risk, so the player engine is isolated
behind one interface (`markerlib/player.py`, `VideoPlayer`). It decodes with Qt
Multimedia's ffmpeg pipeline but **paints the frames itself** from a
`QVideoSink` rather than using `QVideoWidget` — on this machine `QVideoWidget`
showed a blank picture even though decoding was fine, a known Windows quirk. If
scrubbing ever feels janky on the real editing machine, an mpv-based backend can
be dropped in behind the same interface without touching the rest of the app —
ask and it can be added.

---

## Testing

```powershell
.venv\Scripts\python tests\test_core.py          # shared-core unit tests
$env:QT_QPA_PLATFORM="offscreen"; .venv\Scripts\python tests\smoke_marker.py
```

The `sample/` folder contains a generated 45-second synthetic 4K clip and CSVs
used to validate the cutter (stream copy of 4K, the keyframe handle, the `exact`
re-encode path, bad-row skipping, and folder-per-participant). Delete it any time to
reclaim disk space.
```
.venv\Scripts\python cutter.py --video sample\trial_4k.mp4 --csv sample\test_clips.csv --out sample\out
```
