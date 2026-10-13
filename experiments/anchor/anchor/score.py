"""Face identity, measured: how close each clip's face stays to its portrait, and to the anchor's other clips.

Faces are embedded with ArcFace (insightface's buffalo_l models, on the CPU) and compared by cosine
similarity. For ArcFace, about 0.4–0.5 and above is usually the same person, and 0.7 and above very
close; frames without a detectable face are counted, since a lost face is itself a finding.
Needs the `score` extra: uv run --extra score python -m anchor score
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from pathlib import Path

from .plan import Plan
from .store import Store


def parse(clip: Path) -> dict[str, str]:
    """A clip's service, portrait (model and gender) and line, from its name."""
    parts = clip.stem.split("--")
    model, gender = parts[1].rsplit("-", 1)
    return {"phase": clip.parent.name, "service": parts[0], "model": model, "gender": gender,
            "line": parts[2] if len(parts) > 2 else "-"}


def score(plan: Plan, store: Store, frames: int = 8) -> dict:
    import cv2
    import numpy as np
    from insightface.app import FaceAnalysis

    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(640, 640))

    def embed(image) -> np.ndarray | None:
        faces = app.get(image)
        if not faces:
            return None
        face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        return face.normed_embedding

    portraits: dict[str, np.ndarray | None] = {}
    for key, pick in store.picks().items():
        path = store.run / f"portraits/{key}-{pick}.png"
        portraits[key] = embed(cv2.imread(str(path))) if path.exists() else None

    rows, by_anchor = [], defaultdict(list)
    for clip in sorted(store.run.glob("clips/p*/*.mp4")):
        info = parse(clip)
        capture = cv2.VideoCapture(str(clip))
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        embeddings = []
        for i in range(frames):
            capture.set(cv2.CAP_PROP_POS_FRAMES, int((i + 0.5) * total / frames))
            ok, image = capture.read()
            if ok and (e := embed(image)) is not None:
                embeddings.append(e)
        capture.release()
        reference = portraits.get(f"{info['model']}-{info['gender']}")
        sims = [float(np.dot(e, reference)) for e in embeddings] if reference is not None else []
        row = {**info, "clip": str(clip.relative_to(store.run)), "frames": frames, "faces": len(embeddings),
               "to_portrait_mean": round(float(np.mean(sims)), 3) if sims else None,
               "to_portrait_min": round(float(np.min(sims)), 3) if sims else None}
        rows.append(row)
        if embeddings:
            mean = np.mean(embeddings, axis=0)
            by_anchor[(info["service"], info["model"], info["gender"])].append((info["line"], mean / np.linalg.norm(mean)))

    across = []
    for (service, model, gender), clips in sorted(by_anchor.items()):
        if len(clips) < 2:
            continue
        pairs = [float(np.dot(a, b)) for (_, a), (_, b) in itertools.combinations(clips, 2)]
        across.append({"service": service, "model": model, "gender": gender, "clips": len(clips),
                       "between_clips_mean": round(float(np.mean(pairs)), 3),
                       "between_clips_min": round(float(np.min(pairs)), 3)})

    result = {"clips": rows, "across_clips": across}
    (store.run / "scores.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"{'clip':58} faces  to portrait (mean / min)")
    for r in rows:
        print(f"{r['clip']:58} {r['faces']}/{r['frames']}   {r['to_portrait_mean']} / {r['to_portrait_min']}")
    if across:
        print(f"\n{'anchor (service, portrait)':58} between its clips (mean / min)")
        for a in across:
            print(f"{a['service'] + ', ' + a['model'] + '-' + a['gender']:58} {a['between_clips_mean']} / "
                  f"{a['between_clips_min']}  ({a['clips']} clips)")
    return result
