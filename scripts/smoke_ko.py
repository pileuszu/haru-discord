"""Exercise Korean audio input and Haru output locally, without a microphone.

Default input uses macOS synthetic voices. This is a functional check, not a
human conversation quality benchmark. All model calls stay on this thread.
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import mlx.core as mx
import numpy as np
import soundfile as sf
from haru_mlx import DuplexConfig, DuplexSession, HaruModel
from haru_mlx.audio import SAMPLE_RATE, load_audio, prepare_reference
from haru_mlx.prompt import card_templates, render_card

from download_model import ROOT, model_path

QUESTIONS = [
    "오늘 비가 오는데 나갈 때 뭘 챙길까?",
    "빨간색이랑 파란색 중에 하나만 골라 줘.",
    "내 이름은 민수야. 내 이름이 뭐야?",
]
REFERENCE_TEXT = (
    "오늘은 바람이 조금 불어서 창문을 일찍 닫았어요. "
    "잠시 뒤에는 따뜻한 차를 마시면서 조용히 책을 읽었어요."
)


def synthesize(text: str, voice: str, destination: Path) -> np.ndarray:
    subprocess.run(["/usr/bin/say", "-v", voice, "-r", "170", "-o", str(destination), text], check=True)
    audio = load_audio(destination)
    if not len(audio) or not np.isfinite(audio).all() or np.max(np.abs(audio)) < 1e-4:
        raise RuntimeError(f"The system voice produced empty or silent audio: {voice}")
    return audio


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Your own recorded Korean WAV/AIFF instead of synthetic questions")
    parser.add_argument("--reference", type=Path, help="Voice clip you have rights to use (3-180 seconds)")
    parser.add_argument("--tail-seconds", type=float, default=12.0, help="Silence after each question so Haru can answer")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--input-voice", default="Yuna", help="macOS Korean voice for synthetic questions")
    parser.add_argument("--reference-voice", default="Yuna", help="macOS Korean voice for synthetic reference")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not 3 <= args.tail_seconds <= 120:
        parser.error("--tail-seconds must be between 3 and 120")
    if not mx.metal.is_available():
        raise RuntimeError("MLX Metal device is unavailable. Run on Apple Silicon outside a GPU-restricted sandbox.")
    checkpoint = model_path()
    out = args.output_dir or ROOT / "artifacts" / datetime.now().strftime("ko-%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=False)

    reference = prepare_reference(load_audio(args.reference)) if args.reference else prepare_reference(
        synthesize(REFERENCE_TEXT, args.reference_voice, out / "reference.aiff")
    )
    if not 3 * SAMPLE_RATE <= len(reference) <= 180 * SAMPLE_RATE:
        raise ValueError("Reference needs 3-180 seconds of speech after trimming silence.")
    sf.write(out / "reference.wav", reference, SAMPLE_RATE)
    turns = []
    parts = [np.zeros(SAMPLE_RATE, dtype=np.float32)]
    cursor = 1.0
    questions = [None] if args.input else QUESTIONS
    for index, question in enumerate(questions):
        spoken = load_audio(args.input) if args.input else synthesize(
            question, args.input_voice, out / f"question-{index + 1}.aiff"
        )
        if len(spoken) == 0 or not np.isfinite(spoken).all():
            raise ValueError("Input recording must contain finite audio samples.")
        turns.append({"start_seconds": cursor, "end_seconds": cursor + len(spoken) / SAMPLE_RATE, "text": question})
        parts.extend([spoken, np.zeros(round(args.tail_seconds * SAMPLE_RATE), dtype=np.float32)])
        cursor += len(spoken) / SAMPLE_RATE + args.tail_seconds
    user_audio = np.concatenate(parts)
    card = json.loads((ROOT / "discord" / "character-cards.json").read_text(encoding="utf-8"))["ko"]
    prompt = render_card(card, card_templates()["templates"]["ko"]) if card is not None else ""
    (out / "card.txt").write_text(prompt, encoding="utf-8")
    (out / "input.json").write_text(json.dumps({
        "synthetic_input": args.input is None, "synthetic_reference": args.reference is None,
        "input_voice": args.input_voice if args.input is None else None,
        "reference_voice": args.reference_voice if args.reference is None else None,
        "turns": turns, "tail_seconds": args.tail_seconds,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Loading pinned Haru checkpoint with 8-bit backbone. Artifacts: {out}", flush=True)
    started = time.perf_counter()
    model = HaruModel.from_pretrained(str(checkpoint), quantize=8)
    load_seconds = time.perf_counter() - started
    frame_samples = model.config.frame_samples
    count = (len(user_audio) + frame_samples - 1) // frame_samples
    user_audio = np.pad(user_audio, (0, count * frame_samples - len(user_audio)))
    sf.write(out / "input.wav", user_audio, SAMPLE_RATE)
    session = DuplexSession(model, prompt, reference, DuplexConfig(seed=args.seed, max_frames=count + 1))
    print(f"Model loaded in {load_seconds:.1f}s. Processing {count * frame_samples / SAMPLE_RATE:.1f}s of Korean conversation.", flush=True)
    started = time.perf_counter()
    frames = []
    with (out / "transcript.jsonl").open("w", encoding="utf-8") as transcript:
        for index in range(count):
            frame = session.start() if index == 0 else session.step(user_audio[(index - 1) * frame_samples : index * frame_samples])
            frames.append(frame)
            record = {"frame": index, "time": round(index * frame_samples / SAMPLE_RATE, 3), "text": frame.text, "compute_ms": round(frame.seconds * 1000, 3)}
            transcript.write(json.dumps(record, ensure_ascii=False) + "\n")
            transcript.flush()
            if frame.text:
                print(frame.text, end="", flush=True)
            if index and index % 150 == 0:
                print(f"\n[{index}/{count} frames]", flush=True)
    elapsed = time.perf_counter() - started
    reply = np.concatenate([frame.audio for frame in frames])
    if not np.isfinite(reply).all():
        raise RuntimeError("Haru generated non-finite audio samples.")
    sf.write(out / "reply.wav", reply, SAMPLE_RATE)
    sf.write(out / "conversation.wav", np.column_stack([user_audio, reply]), SAMPLE_RATE)
    text = "".join(frame.text for frame in frames).strip()
    (out / "transcript.txt").write_text(text + "\n", encoding="utf-8")
    steps_ms = np.array([frame.seconds * 1000 for frame in frames[1:]])
    rms = float(np.sqrt(np.mean(reply ** 2)))
    metrics = {
        "created_at": datetime.now().astimezone().isoformat(),
        "runtime": json.loads((ROOT / "runtime.json").read_text()),
        "python": platform.python_version(), "mlx": mx.__version__, "seed": args.seed,
        "synthetic_input": args.input is None, "synthetic_reference": args.reference is None,
        "sample_rate": SAMPLE_RATE, "duration_seconds": len(reply) / SAMPLE_RATE,
        "load_seconds": load_seconds, "inference_seconds_including_prefill": elapsed,
        "prefill_seconds": frames[0].seconds,
        "step_ms_p50": float(np.median(steps_ms)), "step_ms_p95": float(np.percentile(steps_ms, 95)),
        "realtime_frame_budget_ms": 1000 * frame_samples / SAMPLE_RATE,
        "peak_mlx_memory_gib": mx.get_peak_memory() / 1024**3,
        "output_rms": rms, "output_peak": float(np.max(np.abs(reply))),
        "output_hangul_characters": len(re.findall(r"[가-힣]", text)),
        "transcript": text,
        "automatic_checks_passed": bool(rms > 1e-4 and re.search(r"[가-힣]", text)),
        "quality_note": "Audio and Hangul presence checks only; inspect semantic responses and listen to the WAV. Not a human conversation benchmark.",
    }
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n" + json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"Results: {out}")
    if not metrics["automatic_checks_passed"]:
        raise SystemExit("Korean functional checks failed. Inspect the generated artifacts.")


if __name__ == "__main__":
    main()
