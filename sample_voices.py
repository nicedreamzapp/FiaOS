"""Load model once, generate a short sample for each voice, play them back."""
import os
import subprocess
import time

import mlx.core as mx
import numpy as np
import rustymimi
import sentencepiece

from personaplex_mlx import models, utils
from personaplex_mlx.persona_utils import (
    DEFAULT_HF_REPO,
    get_lm_config,
    get_or_download_mimi,
    get_or_download_model_file,
    get_or_download_tokenizer,
    get_voice_prompt_dir,
    load_lm_weights,
    resolve_voice_prompt,
    seed_all,
    wrap_with_system_tags,
)

VOICES = [
    "NATF0", "NATF1", "NATF2", "NATF3",
    "NATM0", "NATM1", "NATM2", "NATM3",
    "VARF0", "VARF1", "VARF2", "VARF3", "VARF4",
    "VARM0", "VARM1", "VARM2", "VARM3", "VARM4",
]

PROMPT = "Hey! I'm Fia, your family assistant. How's your day going?"
OUTDIR = os.path.expanduser("~/Desktop/PersonaPlex/voice_samples")
os.makedirs(OUTDIR, exist_ok=True)

# ── Load model once ──
print("Loading model (this takes about a minute)...")
seed_all(42424242)

lm_config = get_lm_config(None, DEFAULT_HF_REPO)
tokenizer_file = get_or_download_tokenizer(DEFAULT_HF_REPO, None)
model_file, _ = get_or_download_model_file(DEFAULT_HF_REPO, 4, None)
mimi_file = get_or_download_mimi(DEFAULT_HF_REPO, None)

text_tokenizer = sentencepiece.SentencePieceProcessor(tokenizer_file)
model = models.Lm(lm_config)
model.set_dtype(mx.bfloat16)
load_lm_weights(model, lm_config, model_file, 4)
print("Model loaded!\n")

voice_prompt_dir = get_voice_prompt_dir(None, DEFAULT_HF_REPO)

# Create 2 seconds of silence as input (24000 Hz * 2s = 48000 samples)
# Shape matches what sphn.read returns: (channels, samples)
silence_pcm = np.zeros((1, 48000), dtype=np.float32)
steps = 48000 // 1920  # 25 steps = ~2s of output

audio_tokenizer = rustymimi.Tokenizer(mimi_file, num_codebooks=8)

# ── Generate each voice ──
print(f"Generating {len(VOICES)} voice samples...\n")

for voice in VOICES:
    outfile = os.path.join(OUTDIR, f"{voice}.wav")
    print(f"  {voice}...", end=" ", flush=True)

    try:
        seed_all(42424242)

        gen = models.LmGen(
            model=model,
            max_steps=100000,
            text_sampler=utils.Sampler(temp=0.7, top_k=25),
            audio_sampler=utils.Sampler(temp=0.8, top_k=250),
            check=False,
            audio_silence_frame_cnt=int(0.5 * 12.5),
        )

        voice_path = resolve_voice_prompt(
            voice=None, voice_prompt=voice, voice_prompt_dir=voice_prompt_dir,
        )
        gen.load_voice_prompt_embeddings(voice_path)
        gen.text_prompt_tokens = text_tokenizer.encode(wrap_with_system_tags(PROMPT))
        gen.reset_streaming()
        gen.step_system_prompts()

        all_out_pcm = []
        for idx in range(steps):
            start = idx * 1920
            end = min((idx + 1) * 1920, 48000)
            pcm_data = silence_pcm[:, start:end]
            if pcm_data.shape[-1] < 1920:
                pad = 1920 - pcm_data.shape[-1]
                pcm_data = np.pad(pcm_data, ((0, 0), (0, pad)), mode="constant")
            encoded = audio_tokenizer.encode_step(pcm_data[None, 0:1])
            tokens = mx.array(encoded).transpose(0, 2, 1)[:, :, :gen.user_codebooks]
            if tokens.shape[1] == gen.user_codebooks and tokens.shape[2] == 1:
                model_input = tokens
            elif tokens.shape[1] == 1 and tokens.shape[2] == gen.user_codebooks:
                model_input = tokens.transpose(0, 2, 1)
            else:
                model_input = tokens

            gen.step(input_tokens=model_input)
            audio_tokens = gen.last_audio_tokens()
            if audio_tokens is not None:
                decode_tokens = np.array(audio_tokens[:, :, None]).astype(np.uint32)
                out_pcm = audio_tokenizer.decode_step(decode_tokens)
                all_out_pcm.append(out_pcm)

        if all_out_pcm:
            combined = np.concatenate(all_out_pcm, axis=-1)
            rustymimi.write_wav(outfile, combined[0, 0], sample_rate=24000)
            print(f"OK ({os.path.getsize(outfile) // 1024}KB)")
        else:
            print("no audio generated")
    except Exception as e:
        print(f"ERROR: {e}")

# ── Play them back ──
print("\n" + "=" * 50)
print("PLAYING ALL VOICES — listen and pick your favorite!")
print("=" * 50 + "\n")

for voice in VOICES:
    outfile = os.path.join(OUTDIR, f"{voice}.wav")
    if os.path.exists(outfile) and os.path.getsize(outfile) > 1000:
        label = "Female" if "F" in voice else "Male"
        style = "Natural" if "NAT" in voice else "Variable"
        print(f"  ▶ {voice} ({style} {label})")
        subprocess.run(["afplay", outfile])
        time.sleep(1)

print("\nDone! Tell me which voice you like best.")
