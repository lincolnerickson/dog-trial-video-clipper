"""Persist the background job queue so an overnight batch survives a crash or an
accidental quit.

The export/join queues normally live only in memory, so if the app died mid-batch
every pending job was lost. Here we write the *unfinished* jobs (the running one
plus everything queued) to a small JSON file on every change; on the next launch
the app offers to resume them. A clean finish clears the file.

Jobs are the same dicts the marker builds. Join jobs are already JSON-native;
export jobs carry Clip objects and CardSpecs, converted here.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from clipper.clips import Clip
from clipper.ffmpeg_tools import CardSpec
from markerlib.applog import app_data_dir

log = logging.getLogger("clipper.session")


def queue_path() -> Path:
    return app_data_dir() / "queue.json"


# ---- job <-> plain-dict conversion --------------------------------------------

def _clip_to_dict(c: Clip) -> dict:
    return {"start": c.start, "end": c.end, "label": c.label,
            "exact": c.exact, "source_participant": c.source_participant}


def _clip_from_dict(d: dict) -> Clip:
    return Clip(d["start"], d["end"], d["label"],
               bool(d.get("exact", False)), d.get("source_participant"))


def _card_to_dict(c):
    return None if c is None else {"image": str(c.image), "seconds": c.seconds}


def _card_from_dict(d):
    return None if not d else CardSpec(image=d["image"], seconds=float(d.get("seconds", 3.0)))


def job_to_dict(job: dict) -> dict:
    """Serialize a marker job dict. 'inputs' distinguishes a join from an export."""
    if "inputs" in job:                       # join job
        return {
            "kind": "join",
            **{k: job[k] for k in
               ("inputs", "out_path", "total", "trim_black", "encoder", "bitrate", "gop", "label")},
        }
    return {                                   # export job
        "kind": "export",
        "video": job["video"],
        "rows": [_clip_to_dict(c) for c in job["rows"]],
        "out_dir": job["out_dir"],
        "folder_per_participant": job["folder_per_participant"],
        "intro": _card_to_dict(job["intro"]),
        "outro": _card_to_dict(job["outro"]),
        "video_mode": job["video_mode"],
        "bitrate": job["bitrate"],
        "label": job["label"],
        "csv": str(job["csv"]) if job.get("csv") else None,
    }


def job_from_dict(d: dict) -> tuple[str, dict]:
    """Rebuild a (kind, job) pair. kind is 'join' or 'export'."""
    if d.get("kind") == "join":
        return "join", {k: d[k] for k in
                        ("inputs", "out_path", "total", "trim_black", "encoder", "bitrate", "gop", "label")}
    return "export", {
        "video": d["video"],
        "rows": [_clip_from_dict(c) for c in d["rows"]],
        "out_dir": d["out_dir"],
        "folder_per_participant": d["folder_per_participant"],
        "intro": _card_from_dict(d.get("intro")),
        "outro": _card_from_dict(d.get("outro")),
        "video_mode": d["video_mode"],
        "bitrate": d["bitrate"],
        "label": d["label"],
        "csv": d.get("csv"),
    }


# ---- file I/O ------------------------------------------------------------------

def save_jobs(jobs: list[dict]) -> None:
    """Write unfinished jobs; an empty list clears the file (clean state)."""
    path = queue_path()
    try:
        if not jobs:
            path.unlink(missing_ok=True)
            return
        payload = {"version": 1, "jobs": [job_to_dict(j) for j in jobs]}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)                     # atomic swap, never a half-written file
    except Exception:
        log.exception("could not persist the job queue")


def load_jobs() -> list[tuple[str, dict]]:
    """Return unfinished (kind, job) pairs from a previous session, or []."""
    path = queue_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [job_from_dict(d) for d in data.get("jobs", [])]
    except Exception:
        log.exception("could not read the saved job queue; ignoring it")
        return []


def clear() -> None:
    save_jobs([])
