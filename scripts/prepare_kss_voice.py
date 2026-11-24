"""Prepare local Korean TTS references from a small, attributed KSS subset.

Downloads individual public Hugging Face preview WAVs, never the full dataset.
KSS is CC BY-NC-SA 4.0 and may only be used noncommercially. The original
publisher describes these professional recordings as intended for Korean TTS.
This does not assert an independent grant of all voice/personality rights.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

import numpy as np
import soundfile as sf
from haru_mlx.audio import SAMPLE_RATE, decode_audio, prepare_reference

ROOT = Path(__file__).resolve().parents[1]
DATASET = "Bingsu/KSS_Dataset"
REVISION = "48fdfd7ab1dbc1a62e4e8a8b9f4c360259d51d3c"
PUBLISHER = "https://www.kaggle.com/datasets/bryanpark/korean-single-speaker-speech-dataset"
LICENSE = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
PAUSE_SECONDS = 0.12


def get_bytes(url: str) -> bytes:
    with urlopen(url, timeout=40) as response:
        return response.read()


def get_json(url: str):
    return json.loads(get_bytes(url))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stats(audio: np.ndarray) -> dict:
    if not audio.size or not np.isfinite(audio).all():
        raise ValueError("Audio must be nonempty and finite")
    peak = float(np.max(np.abs(audio)))
    if peak < 0.0001:
        raise ValueError("Audio is silent")
    return {
        "peak_absolute": peak,
        "rms": float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))),
        "samples_at_or_above_0_999": int(np.count_nonzero(np.abs(audio) >= 0.999)),
        "samples_at_or_beyond_full_scale": int(np.count_nonzero(np.abs(audio) >= 1.0)),
        "finite": True,
    }


def rows_url() -> str:
    return "https://datasets-server.huggingface.co/rows?" + urlencode({
        "dataset": DATASET, "config": "default", "split": "train", "offset": 0, "length": 100,
    })


def fetch_rows() -> dict:
    # Preview API serves current rows. Check the mirror revision before/after
    # fetching, and preserve raw bytes/hashes locally instead of claiming that
    # expiring preview URLs are revision-pinned download links.
    info_url = "https://huggingface.co/api/datasets/" + DATASET
    if get_json(info_url)["sha"] != REVISION:
        raise RuntimeError("KSS mirror revision changed; review source before updating REVISION")
    rows = get_json(rows_url())["rows"]
    if get_json(info_url)["sha"] != REVISION:
        raise RuntimeError("KSS mirror changed during metadata fetch")
    return {item["row_idx"]: item["row"] for item in rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".runtime/voices/kss")
    args = parser.parse_args()
    out = args.output_dir.resolve()
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    prior = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    if prior and prior["source"]["hf_revision_observed"] != REVISION:
        raise RuntimeError("Existing source revision differs from this script")
    cached = {item["row_index"]: item for item in prior["clips"]} if prior else {}
    metadata = None
    selected = []
    waveforms = []
    total_samples = 0
    pause = np.zeros(round(PAUSE_SECONDS * SAMPLE_RATE), dtype=np.float32)
    downloaded_bytes = 0
    for row_index in (sorted(cached) if cached else range(100)):
        previous = cached.get(row_index)
        path = raw_dir / f"train-{row_index:05d}.wav"
        if previous and path.exists():
            data = path.read_bytes()
            if sha256(data) != previous["sha256"]:
                raise RuntimeError(f"Cached source hash mismatch: {path}")
            row = {"original_script": previous["transcript"], "duration": previous["reported_duration_seconds"]}
            asset_url = previous["preview_asset_path"]
        else:
            if metadata is None:
                metadata = fetch_rows()
            row = metadata[row_index]
            if not 2.0 <= row["duration"] <= 10.0:
                continue
            if previous and row["original_script"] != previous["transcript"]:
                raise RuntimeError(f"Source text changed for row {row_index}")
            asset_url = row["audio"][0]["src"]
            if path.exists():
                data = path.read_bytes()
            else:
                data = get_bytes(asset_url)
                downloaded_bytes += len(data)
                path.write_bytes(data)
            if previous and sha256(data) != previous["sha256"]:
                raise RuntimeError(f"Downloaded source hash mismatch for row {row_index}")
        original, source_rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        original_stats = stats(original)
        clip = prepare_reference(decode_audio(data))
        stats(clip)
        if len(clip) < SAMPLE_RATE:
            continue
        added = len(clip) + (len(pause) if waveforms else 0)
        if total_samples + added > 179 * SAMPLE_RATE:
            continue
        parsed = urlsplit(asset_url)
        entry = {
            "row_index": row_index, "raw_file": str(path.relative_to(out)),
            "sha256": sha256(data), "bytes": len(data),
            "transcript": row["original_script"], "reported_duration_seconds": row["duration"],
            "source_sample_rate": int(source_rate), "source_channels": original.shape[1],
            "source_duration_seconds": len(original) / source_rate,
            "source_signal": original_stats,
            "prepared_duration_seconds": len(clip) / SAMPLE_RATE,
            "prepared_signal": stats(clip),
            "preview_asset_path": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
        }
        selected.append(entry)
        waveforms.append(clip)
        total_samples += added
        if total_samples >= 175 * SAMPLE_RATE:
            break
    if not 175 <= total_samples / SAMPLE_RATE <= 179:
        raise RuntimeError(f"Not enough complete, distinct clips: {total_samples / SAMPLE_RATE:.2f}s")

    def write_reference(name: str, clips: list[np.ndarray], entries: list[dict]) -> dict:
        parts = []
        for clip in clips:
            if parts:
                parts.append(pause)
            parts.append(clip)
        audio = np.concatenate(parts)
        # A static gain only if resampling overshoots; no compression, denoising,
        # tempo shift, repeated padding, or cutting a clip to hit a target length.
        gain = min(1.0, 0.98 / float(np.max(np.abs(audio))))
        audio = audio * gain
        target = out / name
        temporary = target.with_suffix(".flac.tmp")
        sf.write(temporary, audio, SAMPLE_RATE, format="FLAC", subtype="PCM_16")
        saved, rate = sf.read(temporary, dtype="float32", always_2d=True)
        info = sf.info(temporary)
        if rate != SAMPLE_RATE or saved.shape[1] != 1 or info.subtype != "PCM_16":
            raise RuntimeError("Unexpected reference format")
        if not 3 <= len(saved) / rate <= 180:
            raise RuntimeError("Reference outside Haru's supported duration")
        temporary.replace(target)
        return {
            "file": name, "duration_seconds": len(saved) / rate,
            "sha256": sha256(target.read_bytes()), "sample_rate": rate,
            "channels": 1, "subtype": info.subtype, "row_indices": [entry["row_index"] for entry in entries],
            "linear_gain": gain, "signal": stats(saved),
        }

    short_count = 0
    short_samples = 0
    for clip in waveforms:
        short_samples += len(clip) + (len(pause) if short_count else 0)
        short_count += 1
        if short_samples >= 28 * SAMPLE_RATE:
            break
    outputs = [
        write_reference("ko-kss-30s.flac", waveforms[:short_count], selected[:short_count]),
        write_reference("ko-kss-3min.flac", waveforms, selected),
    ]
    manifest = {
        "schema_version": 1,
        "created_at_utc": prior["created_at_utc"] if prior else datetime.now(timezone.utc).isoformat(),
        "source": {
            "title": "KSS Dataset: Korean Single Speaker Speech Dataset", "creator": "Kyubyong Park",
            "year": 2018, "original_publisher": PUBLISHER,
            "mirror": "https://huggingface.co/datasets/" + DATASET,
            "hf_revision_observed": REVISION, "config": "default", "split": "train",
            "metadata_api": rows_url(), "speaker": "Single professional female voice actress, as described by the original publisher",
            "license": "CC BY-NC-SA 4.0", "license_url": LICENSE,
            "use": "Local noncommercial Korean speech synthesis evaluation",
            "rights_note": "Publisher explicitly describes Korean TTS use. CC licensing does not independently grant all personality, privacy, or publicity rights; do not impersonate or imply endorsement.",
            "reproduction": "Reuse raw WAVs verified by SHA-256. If missing, refresh per-row preview URLs only while mirror revision matches. Preview URL signatures expire and are not provenance identifiers.",
        },
        "transformations": {
            "decode": "soundfile float32 decode; channel mean if needed; scipy resample_poly via haru_mlx.audio.decode_audio to 24000 Hz",
            "trim": "haru_mlx.audio.prepare_reference removes boundary silence below -45 dBFS with 200 ms margins; each source under 10 s, no 180 s truncation",
            "assembly": "Distinct complete sentence clips from one speaker in dataset row order, no repetition or mid-word cut",
            "inter_clip_silence_seconds": PAUSE_SECONDS,
            "amplitude": "Static linear gain only if peak exceeds 0.98; gain recorded per output",
            "format": "24 kHz mono FLAC PCM16", "noise_processing": "None", "speed_change": "None",
        },
        "clips": selected, "outputs": outputs,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    (out / "README.md").write_text(
        "# KSS Korean reference clips\n\n"
        "KSS Dataset: Korean Single Speaker Speech Dataset — Kyubyong Park, 2018.\n\n"
        f"Original publisher: {PUBLISHER}\n\n"
        f"Mirror: https://huggingface.co/datasets/{DATASET}/tree/{REVISION}\n\n"
        f"License: CC BY-NC-SA 4.0 — {LICENSE}\n\n"
        "For noncommercial use only. Retain attribution, license and modification notices; distribute adapted source audio under the same license. "
        "The original publisher describes a professional female voice actress recorded for Korean TTS. "
        "This does not independently establish permission for every personality/publicity use or imply speaker endorsement.\n\n"
        "Changes: same-speaker complete sentence clips decoded, resampled to 24 kHz mono, boundary silence trimmed using Haru, "
        "joined with 120 ms silence and saved as PCM16 FLAC. A small static gain is used only if required to avoid clipping. "
        "No repeated sentences, denoising, speed changes or mid-word truncation. See manifest.json for exact row IDs, transcripts, hashes and durations.\n\n"
        "The approximately 30-second and 3-minute references are comparison candidates; longer does not establish better pronunciation. "
        "Raw clips are cached for reproducibility. Audio remains local and is not included in Git.\n"
    )
    print(json.dumps({"output_dir": str(out), "downloaded_bytes_this_run": downloaded_bytes,
                      "unique_source_clips": len(selected), "references": outputs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
