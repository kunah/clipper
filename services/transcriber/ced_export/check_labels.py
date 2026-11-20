"""Build check of the CED export's labels.

    python check_labels.py /out/id2label.json

export_onnx.py writes the model's label names (id2label.json). CED's Hub code reads them from
topel/ConvNeXt-Tiny-AT's class_labels_indices.csv on that repo's unpinned main branch, at build
time. The graph doesn't depend on them, but the transcriber's sound groups read outputs by
AudioSet index (`clipper_transcriber.ced.AUDIOSET_INDEX`). So the 527 labels must have every group
label at its pinned index (group_labels.json, a copy of AUDIOSET_INDEX that a test keeps equal);
anything else fails the build. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

AUDIOSET_LABELS = 527


def check(id2label: dict[str, str], expected: dict[str, str]) -> list[str]:
    """Problems found (empty = fine)."""
    problems = []
    if len(id2label) != AUDIOSET_LABELS:
        problems.append(f"expected {AUDIOSET_LABELS} labels, got {len(id2label)}")
    for index, label in expected.items():
        if id2label.get(index) != label:
            problems.append(f"index {index}: expected {label!r}, got {id2label.get(index)!r}")
    return problems


def main() -> None:
    id2label = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    expected = json.loads(Path(__file__).with_name("group_labels.json").read_text(encoding="utf-8"))
    problems = check({str(k): str(v) for k, v in id2label.items()}, expected)
    if problems:
        raise SystemExit("CED labels not at the pinned AudioSet indices: " + "; ".join(problems))
    print(f"CED labels: {len(expected)} group labels at their AudioSet indices")


if __name__ == "__main__":
    main()
