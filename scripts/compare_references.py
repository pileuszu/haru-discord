"""Compare voice references with one local Haru load and fixed Korean input.

References and seeds vary within an invocation; prompt and settings stay fixed.
Saved model text is not an independent
transcription of the output WAV: pronunciation must be assessed by listening.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import re
import time
from datetime import datetime
from itertools import product
from pathlib import Path

import mlx.core as mx
import numpy as np
import soundfile as sf
from haru_mlx import DuplexConfig, DuplexSession, HaruModel
from haru_mlx.audio import SAMPLE_RATE, load_audio, prepare_reference
from haru_mlx.prompt import CARD_FIELDS, card_templates, render_card

from download_model import ROOT, model_path

QUALITY_NOTE = (
    "Input, prompt, model, and sampling settings are fixed within this invocation; "
    "references and recorded seeds may vary. "
    "Generated text is not ASR of the output audio. RMS, peak, and Hangul checks "
    "do not assess pronunciation or comprehension; listen to reply.wav files."
)


def reference_argument(value: str) -> tuple[str, Path]:
    label, separator, filename = value.partition("=")
    if not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", label) or not filename:
        raise argparse.ArgumentTypeError("expected LABEL=PATH; label uses letters, digits, _ or -")
    return label, Path(filename).expanduser().resolve()


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_audio(audio: np.ndarray, name: str) -> None:
    if not len(audio) or not np.isfinite(audio).all():
        raise ValueError(f"{name} must contain finite audio samples")
    if float(np.max(np.abs(audio))) < 1e-4:
        raise ValueError(f"{name} is silent")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", action="append", required=True, type=reference_argument,
                        metavar="LABEL=PATH", help="repeat for each voice reference you have rights to use")
    parser.add_argument("--input", type=Path, required=True,
                        help="input speech file to replay for every reference and seed")
    parser.add_argument("--output-dir", type=Path, required=True, help="new directory; never overwrite existing results")
    parser.add_argument("--tail-seconds", type=float, default=12.0)
    parser.add_argument("--quantize", choices=("8", "none"), default="8",
                        help="8-bit backbone or published bfloat16 weights; default: 8")
    parser.add_argument("--eop-bias", type=float, default=0.0, help="offline EOP logit bias, from -2 to 2")
    parser.add_argument("--card-json", type=Path, help="Korean character card JSON object, or null for no system prompt")
    parser.add_argument("--seed", type=int, action="append", help="repeat for multiple seeds; default: 0")
    parser.add_argument("--prepared-input", action="store_true",
                        help="input already includes timing/silence; add no leading or trailing silence")
    args = parser.parse_args()
    quantize = None if args.quantize == "none" else int(args.quantize)
    precision = "bf16" if quantize is None else f"int{quantize}"
    if not np.isfinite(args.tail_seconds) or not 3 <= args.tail_seconds <= 120:
        parser.error("--tail-seconds must be between 3 and 120")
    if not np.isfinite(args.eop_bias) or not -2 <= args.eop_bias <= 2:
        parser.error("--eop-bias must be finite and between -2 and 2")
    card = json.loads((ROOT / "discord" / "character-cards.json").read_text(encoding="utf-8"))["ko"]
    if args.card_json:
        try:
            card = json.loads(args.card_json.expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            parser.error(f"cannot load --card-json: {error}")
        if card is not None and (
            not isinstance(card, dict) or not card or not card.keys() <= set(CARD_FIELDS)
            or not all(isinstance(value, str) for value in card.values())
        ):
            parser.error("--card-json must be null or a nonempty card with recognized fields and string values")
    seeds = args.seed if args.seed is not None else [0]
    if any(not 0 <= seed <= 0xFFFFFFFF for seed in seeds):
        parser.error("--seed must be an unsigned 32-bit integer")
    if len(set(seeds)) != len(seeds):
        parser.error("seeds must be unique")
    labels = [label for label, _ in args.reference]
    if len(set(labels)) != len(labels):
        parser.error("reference labels must be unique")
    if not mx.metal.is_available():
        raise RuntimeError("MLX Metal is unavailable; run outside the GPU-restricted sandbox on Apple Silicon")

    source_path = args.input.expanduser().resolve()
    source_audio = load_audio(source_path)
    validate_audio(source_audio, "Input")
    user_audio = source_audio.copy() if args.prepared_input else np.concatenate([
        np.zeros(SAMPLE_RATE, dtype=np.float32), source_audio,
        np.zeros(round(args.tail_seconds * SAMPLE_RATE), dtype=np.float32),
    ])
    reference_limit = DuplexConfig().reference_seconds
    prepared = []
    for label, path in args.reference:
        source_reference = load_audio(path)
        validate_audio(source_reference, f"Reference {label}")
        reference = prepare_reference(source_reference)
        if not 3 * SAMPLE_RATE <= len(reference) <= 180 * SAMPLE_RATE:
            raise ValueError(f"Reference {label} needs 3-180 seconds after silence trimming")
        prepared.append((label, path, reference, len(source_reference) / SAMPLE_RATE))

    checkpoint = model_path()  # Pinned, local-only checkpoint; no model downloads.
    prompt = render_card(card, card_templates()["templates"]["ko"]) if card is not None else ""
    prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
    out = args.output_dir.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=False)  # Atomically reserve a fresh result directory.
    (out / "card.txt").write_text(prompt + "\n", encoding="utf-8")
    summary = {
        "created_at": datetime.now().astimezone().isoformat(),
        "status": "running", "runtime": json.loads((ROOT / "runtime.json").read_text()),
        "python": platform.python_version(), "mlx": mx.__version__, "quantize": quantize, "precision": precision,
        "input_path": str(source_path), "input_file_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "input_speech_seconds": len(source_audio) / SAMPLE_RATE,
        "prepared_input": args.prepared_input,
        "input_duration_seconds": len(source_audio) / SAMPLE_RATE,
        "leading_silence_seconds": 0.0 if args.prepared_input else 1.0,
        "tail_seconds": 0.0 if args.prepared_input else args.tail_seconds,
        "seed": seeds[0] if len(seeds) == 1 else None, "seeds": seeds,
        "guidance_scale": 1.5, "speak_first": False, "eop_bias": args.eop_bias,
        "reference_seconds_limit": reference_limit, "sample_rate": SAMPLE_RATE,
        "card": card, "system_prompt": prompt, "prompt_sha256": prompt_sha256,
        "card_json_path": str(args.card_json.expanduser().resolve()) if args.card_json else None,
        "quality_note": QUALITY_NOTE, "results": [],
    }
    write_json(out / "summary.json", summary)
    print(f"Loading pinned Haru {precision} once. Results: {out}", flush=True)
    load_started = time.perf_counter()
    model = HaruModel.from_pretrained(str(checkpoint), quantize=quantize)
    summary["load_seconds"] = time.perf_counter() - load_started
    frame_samples = model.config.frame_samples
    input_frames = (len(user_audio) + frame_samples - 1) // frame_samples
    user_audio = np.pad(user_audio, (0, input_frames * frame_samples - len(user_audio)))
    # Frame 0 comes from the prompt. Consuming every input frame produces one
    # additional model frame. Pad the user channel by one silent frame so the
    # stereo artifact preserves the live timeline without dropping the last input.
    aligned_user = np.pad(user_audio, (0, frame_samples))
    sf.write(out / "input.wav", user_audio, SAMPLE_RATE)
    summary.update({"frame_samples": frame_samples, "input_frames": input_frames,
                    "output_frames_per_reference": input_frames + 1})

    for (label, path, reference, source_seconds), seed in product(prepared, seeds):
        run_label = label if len(seeds) == 1 else f"{label}-seed{seed}"
        result_dir = out / run_label
        result_dir.mkdir()
        gc.collect()
        mx.clear_cache()
        mx.reset_peak_memory()
        config = DuplexConfig(seed=seed, guidance_scale=1.5, speak_first=False, eop_bias=args.eop_bias,
                              max_frames=input_frames + 1)
        session = DuplexSession(model, prompt, reference, config)
        effective = session.reference
        effective_seconds = len(effective) / SAMPLE_RATE if effective is not None else 0.0
        sf.write(result_dir / "reference.wav", effective, SAMPLE_RATE)
        print(f"{run_label}: reference {effective_seconds:.2f}s, {input_frames + 1} output frames", flush=True)
        frames = []
        started = time.perf_counter()
        with (result_dir / "transcript.jsonl").open("w", encoding="utf-8") as transcript:
            for index in range(input_frames + 1):
                frame = session.start() if index == 0 else session.step(
                    user_audio[(index - 1) * frame_samples:index * frame_samples]
                )
                frames.append(frame)
                transcript.write(json.dumps({
                    "frame": frame.index, "time_seconds": frame.index * frame_samples / SAMPLE_RATE,
                    "text": frame.text, "compute_ms": frame.seconds * 1000,
                }, ensure_ascii=False) + "\n")
                transcript.flush()
                if index and index % 75 == 0:
                    print(f"{run_label}: {index}/{input_frames} input frames", flush=True)
        elapsed = time.perf_counter() - started
        reply = np.concatenate([frame.audio for frame in frames])
        if not np.isfinite(reply).all():
            raise RuntimeError(f"{run_label}: non-finite output audio")
        sf.write(result_dir / "reply.wav", reply, SAMPLE_RATE)
        sf.write(result_dir / "conversation.wav", np.column_stack([aligned_user, reply]), SAMPLE_RATE)
        text = "".join(frame.text for frame in frames).strip()
        (result_dir / "transcript.txt").write_text(text + "\n", encoding="utf-8")
        step_ms = np.array([frame.seconds * 1000 for frame in frames[1:]])
        rms = float(np.sqrt(np.mean(reply ** 2)))
        metrics = {
            "label": label, "run_label": run_label, "reference_path": str(path),
            "quantize": quantize, "precision": precision,
            "reference_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "reference_source_seconds": source_seconds,
            "reference_prepared_seconds": len(reference) / SAMPLE_RATE,
            "reference_effective_seconds": effective_seconds,
            "reference_seconds_limit": config.reference_seconds,
            "seed": seed, "guidance_scale": config.guidance_scale, "eop_bias": config.eop_bias,
            "card": card, "system_prompt": prompt, "prompt_sha256": prompt_sha256,
            "input_frames_consumed": input_frames, "output_frames": len(frames),
            "output_seconds": len(reply) / SAMPLE_RATE,
            "inference_seconds_including_prefill": elapsed,
            "prefill_seconds": frames[0].seconds, "prompt_tokens": session.prompt_length,
            "step_ms_p50": float(np.median(step_ms)), "step_ms_p95": float(np.percentile(step_ms, 95)),
            "realtime_frame_budget_ms": frame_samples / SAMPLE_RATE * 1000,
            "peak_mlx_memory_gib": mx.get_peak_memory() / 1024 ** 3,
            "output_rms": rms, "output_peak": float(np.max(np.abs(reply))),
            "output_hangul_characters": len(re.findall(r"[가-힣]", text)),
            "output_nonwhitespace_characters": sum(not char.isspace() for char in text),
            "text_bearing_frames": sum(bool(frame.text.strip()) for frame in frames),
            "transcript": text,
            "functional_checks_passed": bool(rms > 1e-4 and re.search(r"[가-힣]", text)),
            "quality_note": QUALITY_NOTE,
        }
        write_json(result_dir / "metrics.json", metrics)
        summary["results"].append(metrics)
        write_json(out / "summary.json", summary)
        print(json.dumps(metrics, ensure_ascii=False), flush=True)
        del session, frames, frame, reply, effective
        gc.collect()
        mx.clear_cache()

    summary["status"] = "complete"
    summary["functional_checks_passed"] = all(row["functional_checks_passed"] for row in summary["results"])
    write_json(out / "summary.json", summary)
    print(f"Comparison complete: {out / 'summary.json'}", flush=True)
    print(QUALITY_NOTE, flush=True)
    if not summary["functional_checks_passed"]:
        raise SystemExit("At least one reference failed the audio/Hangul functional check; inspect saved results")


if __name__ == "__main__":
    main()
