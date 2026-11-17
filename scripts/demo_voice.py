"""Prepare a local synthetic Korean reference for the native app, without recording."""
from pathlib import Path
from importlib import resources

import soundfile as sf
from haru_mlx.audio import SAMPLE_RATE, prepare_reference
from download_model import ROOT
from smoke_ko import REFERENCE_TEXT, synthesize

voice_path = Path(str(resources.files("haru_mlx") / "assets" / "voices" / "ko.flac"))
if voice_path.exists():
    print(f"Keeping the existing Korean reference: {voice_path}")
else:
    source = ROOT / ".runtime" / "demo-voice.aiff"
    audio = prepare_reference(synthesize(REFERENCE_TEXT, "Yuna", source))
    if len(audio) < 3 * SAMPLE_RATE:
        raise RuntimeError("Synthetic reference must contain at least 3 seconds of speech.")
    voice_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(voice_path, audio, SAMPLE_RATE)
    print(f"Local synthetic Yuna reference prepared: {voice_path}")
