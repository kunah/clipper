"""Export mispeech/ced-base (pinned revision) to ONNX with the mel frontend inside the graph.

    python export_onnx.py --dtype fp32|fp16 --out /work/models/ced-base-fp32.onnx

Provenance: written for the CED-on-GPU evaluation (2026-11-19/20; the main checkout's
data/bench/ced-onnx/RESULTS.md, DESIGN.md §16) and copied here unchanged apart from this header
and formatting. The transcriber image runs it in its `ced-export` build stage
(services/transcriber/Dockerfile) and keeps only the .onnx; the build fails unless the file's
sha256 is the evaluated one (`clipper_transcriber.ced.CED_ONNX_SHA256`).

The bytes are reproducible with the pinned toolchain in requirements.txt (Python 3.12, CPU torch
2.15.0 + torchaudio 2.11.0, transformers 5.23.0, onnxscript 0.7.2, onnx 1.23.2; CUDA torch gives
the same bytes):

    --dtype fp16  172,665,049 bytes
                  sha256 76cbe72ec8aa39d1291fcdb1bbd722cfa3be5b99a932ba7d85428478c46a6dc6
    --dtype fp32  344,088,386 bytes (only for a CPU mode, not built: fp16 is slow on the CPU)
                  sha256 a842a0c6e3e014aa5fb42cc381c785ed0f74d627262ef3ce996d32cf0672c11a

To run it by hand (any OS, CPU is enough; downloads ~345 MB from the Hugging Face Hub):

    uv venv .venv-ced-export --python 3.12
    uv pip install --python .venv-ced-export -r services/transcriber/ced_export/requirements.txt \
        --index https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match
    .venv-ced-export/bin/python services/transcriber/ced_export/export_onnx.py \
        --dtype fp16 --out ced-base-fp16.onnx

Runs where torch + torchaudio + transformers + onnxscript are installed (CPU is enough).
Graph: waveform [batch, samples] float32 (16 kHz mono) -> probs [batch, 527] float32.

The frontend reproduces the Hub feature extractor (torchaudio MelSpectrogram: n_fft 512,
hann window (periodic) 512, hop 160, center + reflect pad, power 2, 64 HTK mel bands 0-8000 Hz,
no norm; AmplitudeToDB power, amin 1e-10, ref 1, top_db 120) with ops every execution provider
has: the STFT is a Conv1d with a windowed DFT basis. torchaudio's top_db floor on a
[batch, 64, frames] tensor is the max over the WHOLE batch minus 120 dB; the graph does the same
so its output equals the reference for the same batch. fp16: the frontend and the BatchNorm input
stay float32 (power spectra overflow float16), the transformer runs in float16, output float32.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import torch
import torch.nn.functional as F
import torchaudio
from torch import nn
from transformers import AutoModelForAudioClassification

REPO = "mispeech/ced-base"
REVISION = "db3e14a8db4c21b56b165261c39649741a900e7f"
SR, N_FFT, HOP, N_MELS = 16000, 512, 160, 64


class MelFrontend(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        ref = torchaudio.transforms.MelSpectrogram(
            sample_rate=SR,
            n_fft=N_FFT,
            win_length=N_FFT,
            hop_length=HOP,
            f_min=0,
            f_max=None,
            n_mels=N_MELS,
            center=True,
        )
        fb = ref.mel_scale.fb.detach().clone().float()  # [257, 64], HTK, norm None
        n_bins = N_FFT // 2 + 1
        window = torch.hann_window(N_FFT, periodic=True, dtype=torch.float64)
        k = torch.arange(n_bins, dtype=torch.float64)[:, None]
        n = torch.arange(N_FFT, dtype=torch.float64)[None, :]
        ang = 2 * math.pi * k * n / N_FFT
        basis = torch.cat([torch.cos(ang) * window, -torch.sin(ang) * window])  # [514, 512]
        self.register_buffer("basis", basis.float()[:, None, :])
        self.register_buffer("fb", fb)
        self.n_bins = n_bins

    def forward(self, wav: torch.Tensor) -> torch.Tensor:
        x = F.pad(wav[:, None, :], (N_FFT // 2, N_FFT // 2), mode="reflect")
        spec = F.conv1d(x, self.basis, stride=HOP)  # [B, 514, frames]
        re, im = spec[:, : self.n_bins], spec[:, self.n_bins :]
        power = re * re + im * im
        mel = torch.matmul(power.transpose(1, 2), self.fb).transpose(1, 2)  # [B, 64, frames]
        db = 10.0 * torch.log10(torch.clamp(mel, min=1e-10))
        floor = db.amax(dim=(0, 1, 2), keepdim=True) - 120.0  # batch-wide, like torchaudio
        return torch.maximum(db, floor)


class CedOnnx(nn.Module):
    def __init__(self, model: nn.Module, half: bool) -> None:
        super().__init__()
        self.frontend = MelFrontend()
        self.model = model
        self.half_ = half

    def forward(self, waveform: torch.Tensor) -> torch.Tensor:
        feats = self.frontend(waveform)
        if self.half_:
            feats = feats.half()
        return self.model(input_values=feats).logits.float()  # already sigmoid-ed


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dtype", choices=["fp32", "fp16"], default="fp32")
    p.add_argument("--out", required=True)
    p.add_argument("--device", default="cpu")
    p.add_argument("--opset", type=int, default=18)
    args = p.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    model = AutoModelForAudioClassification.from_pretrained(
        REPO, revision=REVISION, trust_remote_code=True
    ).eval()
    half = args.dtype == "fp16"
    if half:
        model = model.half()
    wrapper = CedOnnx(model, half).to(args.device).eval()

    # Self-check of the frontend against the Hub feature extractor (fp32, CPU).
    from transformers import AutoFeatureExtractor

    fe = AutoFeatureExtractor.from_pretrained(REPO, revision=REVISION, trust_remote_code=True)
    g = torch.Generator().manual_seed(0)
    test = (torch.randn(4, 2 * SR, generator=g) * 0.1).float()
    test[3] = 0.0  # digital silence: exercises the top_db floor
    ref = fe(test, sampling_rate=SR, return_tensors="pt")["input_values"]
    ours = MelFrontend()(test)
    frontend_diff = float((ref - ours).abs().max())
    print("frontend max |dB diff| vs Hub feature extractor:", frontend_diff)
    if frontend_diff > 1e-3:  # measured 3e-5 dB with the pinned toolchain
        raise SystemExit(f"frontend differs from the Hub feature extractor by {frontend_diff} dB")

    batch = torch.export.Dim("batch", min=1, max=1024)
    # The encoder splits inputs longer than 1012 frames (~10.1 s): keep the graph below that.
    samples = torch.export.Dim("samples", min=N_FFT, max=1011 * HOP)
    example = (torch.randn(2, 2 * SR) * 0.1).to(args.device)
    with torch.inference_mode():
        onnx_program = torch.onnx.export(
            wrapper,
            (example,),
            input_names=["waveform"],
            output_names=["probs"],
            dynamic_shapes={"waveform": {0: batch, 1: samples}},
            opset_version=args.opset,
            dynamo=True,
            external_data=False,
        )
    onnx_program.save(str(out), external_data=False)
    # Reproducible bytes: the exporter stores per-node metadata (source locations, which
    # include the venv path) and the torch build ("+cpu"/"+cu132") in the file. Drop both so
    # the same pinned toolchain gives the same sha256 whichever torch build or path ran it.
    import onnx

    proto = onnx.load(str(out))
    for node in proto.graph.node:
        del node.metadata_props[:]
        node.doc_string = ""
    proto.producer_version = torch.__version__.split("+")[0]
    onnx.save(proto, str(out))
    onnx.checker.check_model(str(out))
    labels = {int(k): v for k, v in model.config.id2label.items()}
    (out.parent / "id2label.json").write_text(json.dumps(labels))
    meta = {
        "file": out.name,
        "bytes": out.stat().st_size,
        "sha256": sha256(out),
        "source": f"{REPO}@{REVISION} model.safetensors",
        "dtype": args.dtype,
        "torch": torch.__version__,
        "torchaudio": torchaudio.__version__,
        "opset": args.opset,
    }
    try:
        import onnxscript

        meta["onnxscript"] = onnxscript.__version__
    except Exception:
        pass
    (out.with_suffix(".json")).write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
