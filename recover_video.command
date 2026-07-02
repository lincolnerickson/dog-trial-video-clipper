#!/bin/bash
# Dog Trial Video Clipper — video recovery helper (macOS)
#
# Repairs an encoded/joined video that won't open because the app (or the Mac)
# quit while ffmpeg was still writing it. The footage is almost always intact;
# what's missing is the MP4 index (the "moov atom") written at the very end.
# This script rebuilds that index with the open-source `untrunc` tool using a
# healthy video made with the same settings as a template — no re-encoding,
# so it takes minutes, not hours.
#
# HOW TO USE: double-click this file. If macOS says it can't be opened because
# it's from the internet, right-click (or Control-click) it and choose Open.
#
# IMPORTANT: run this BEFORE reopening the Dog Trial Video Clipper app.
# When the app reopens it resumes the interrupted job and will OVERWRITE the
# broken file. (This script makes a safety copy first, just in case.)

set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

say_step()  { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
fail()      { printf '\n\033[31mSTOPPED: %s\033[0m\n' "$1"; read -r -p "Press Return to close..." _; exit 1; }

pick_file() {  # $1 = dialog prompt; prints POSIX path or nothing if cancelled
    osascript -e "POSIX path of (choose file with prompt \"$1\")" 2>/dev/null
}

RECOVERY_DIR="$HOME/Desktop/Clipper Recovery"

echo "This tool repairs a Dog Trial Video Clipper output file that won't open."
echo "Recovered files will be placed in:  $RECOVERY_DIR"
echo
read -r -p "Press Return to begin (or close this window to cancel)..." _

# ---------------------------------------------------------------- ffmpeg
# The Clipper app bundles its own ffmpeg; use it for diagnosis + final cleanup.
say_step "Locating ffmpeg"
FFMPEG="$(command -v ffmpeg || true)"
if [ -z "$FFMPEG" ]; then
    FFMPEG="$(find "/Applications/Dog Trial Video Clipper.app" -type f -name "ffmpeg*" -perm -u+x 2>/dev/null | head -n1)"
fi
if [ -n "$FFMPEG" ]; then
    echo "Using ffmpeg: $FFMPEG"
else
    echo "ffmpeg not found — recovery can still run, but with fewer checks."
fi

# ------------------------------------------------------------ broken file
say_step "Select the video that won't open"
BROKEN="$(pick_file "Select the video that will NOT open (the interrupted one)")"
[ -n "$BROKEN" ] || fail "no file selected."
echo "Broken file: $BROKEN"

if [ -n "$FFMPEG" ]; then
    DIAG="$("$FFMPEG" -hide_banner -i "$BROKEN" 2>&1 || true)"
    if echo "$DIAG" | grep -qi "moov atom not found"; then
        echo "Confirmed: the file is missing its index (moov atom) — recoverable."
    elif echo "$DIAG" | grep -q "Duration:"; then
        echo
        echo "This file actually has a valid index — the problem may be something"
        echo "else (codec support in the player, permissions, a bad copy)."
        read -r -p "Continue with recovery anyway? [y/N] " GO
        case "$GO" in [Yy]*) ;; *) fail "nothing to repair." ;; esac
    fi
fi

# ------------------------------------------------------------- safety copy
say_step "Making a safety copy"
mkdir -p "$RECOVERY_DIR" || fail "could not create $RECOVERY_DIR"
BASE="$(basename "$BROKEN")"
SAFE="$RECOVERY_DIR/$BASE"
if [ -e "$SAFE" ]; then
    echo "Safety copy already exists — reusing it."
else
    echo "Copying (a big file takes a few minutes)..."
    cp "$BROKEN" "$SAFE" || fail "copy failed — is there enough disk space?"
fi

# --------------------------------------------------------- reference video
say_step "Select a healthy reference video"
echo "untrunc needs one HEALTHY video made by the SAME app with the same"
echo "settings — e.g. an earlier overnight encode that finished normally."
echo "(If you don't have one, this script can build a short one from an"
echo "original camera file instead.)"
if [ -n "$FFMPEG" ]; then
    read -r -p "Do you have a healthy previous output file? [Y/n] " HAVE_REF
else
    HAVE_REF=Y
fi
case "${HAVE_REF:-Y}" in
    [Nn]*)
        SRC="$(pick_file "Select ONE of the ORIGINAL camera files from that recording")"
        [ -n "$SRC" ] || fail "no file selected."
        REF="$RECOVERY_DIR/reference_temp.mp4"
        echo "Encoding a 60-second reference clip (1-2 minutes)..."
        "$FFMPEG" -hide_banner -loglevel error -y -t 60 -i "$SRC" \
            -map 0:v:0 -map "0:a?" \
            -c:v hevc_videotoolbox -b:v 4000k -tag:v hvc1 -c:a aac \
            "$REF" || fail "could not build a reference clip."
        ;;
    *)
        REF="$(pick_file "Select a HEALTHY video previously made by the Clipper app")"
        [ -n "$REF" ] || fail "no file selected."
        ;;
esac
echo "Reference: $REF"

# ---------------------------------------------------------------- untrunc
say_step "Getting the repair tool (untrunc)"
if ! command -v untrunc >/dev/null 2>&1; then
    if ! command -v brew >/dev/null 2>&1; then
        echo "This needs Homebrew (the standard Mac package manager) to install"
        echo "untrunc. Installing Homebrew asks for your Mac login password."
        read -r -p "Install Homebrew now? [y/N] " OK
        case "$OK" in [Yy]*) ;; *) fail "untrunc is required; see https://brew.sh" ;; esac
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
            || fail "Homebrew install failed."
        export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
    fi
    echo "Installing untrunc..."
    brew install untrunc || fail "could not install untrunc."
fi

# ----------------------------------------------------------------- repair
say_step "Repairing (no re-encoding — this is the fast part)"
cd "$RECOVERY_DIR" || fail "cannot enter $RECOVERY_DIR"
untrunc "$REF" "$SAFE" || fail "untrunc could not repair this file."

FIXED="$(ls -t "$RECOVERY_DIR"/*_fixed* 2>/dev/null | head -n1)"
[ -n "$FIXED" ] || fail "untrunc finished but no repaired file was found."

# Clean container pass: rebuilds timestamps and puts the index up front so the
# file scrubs smoothly. Stream copy only — still no re-encoding.
FINAL="$RECOVERY_DIR/${BASE%.*} (recovered).mp4"
if [ -n "$FFMPEG" ]; then
    echo "Finalizing container..."
    if "$FFMPEG" -hide_banner -loglevel error -y -i "$FIXED" \
        -map 0:v:0 -map "0:a?" -c copy -movflags +faststart "$FINAL"; then
        rm -f "$FIXED"
    else
        mv "$FIXED" "$FINAL"   # remux failed; the raw untrunc output still plays
    fi
else
    mv "$FIXED" "$FINAL"
fi
rm -f "$RECOVERY_DIR/reference_temp.mp4"

say_step "Done"
echo "Recovered video: $FINAL"
echo "It contains everything encoded before the interruption (the last few"
echo "seconds may be missing). Please play it through before deleting anything."
open "$RECOVERY_DIR"
read -r -p "Press Return to close..." _
