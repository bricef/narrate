"""The review page: every portrait and clip, side by side, with the services and models hidden.

Letters stand for the image models and avatar services, shuffled the same way every time for a run, so
judging isn't swayed by names. The page shows the files through links named only with those letters, so
a file's own name (which names its service) can't give it away. The key is in key.json beside the page;
--reveal writes the names in.
"""

from __future__ import annotations

import html
import json
import random
from pathlib import Path

from .plan import GENDERS, Plan
from .score import parse
from .store import Store


def letters(names: list[str], seed: str, prefix: str) -> dict[str, str]:
    shuffled = sorted(names)
    random.Random(seed + prefix).shuffle(shuffled)
    return {name: f"{prefix}{i + 1}" for i, name in enumerate(shuffled)}


def review(plan: Plan, store: Store, reveal: bool = False) -> Path:
    run, out = store.run, store.run / "review"
    out.mkdir(parents=True, exist_ok=True)
    models = letters(list(plan.data["image_models"]), str(plan.data["run"]), "Portraits ")
    services = letters([*plan.data["avatars"], *plan.data["phases"].get("4", {}).get("video", {})],
                       str(plan.data["run"]), "Service ")
    (out / "key.json").write_text(json.dumps({"image models": models, "services": services}, indent=2) + "\n")
    scores = {}
    if (run / "scores.json").exists():
        scores = {r["clip"]: r for r in json.loads((run / "scores.json").read_text())["clips"]}

    def label(name: str, table: dict[str, str]) -> str:
        return f"{table[name]} ({name})" if reveal else table[name]

    linked = out / "media"
    if linked.exists():
        for old in linked.iterdir():
            old.unlink()
    linked.mkdir(exist_ok=True)

    def blind_name(path: Path) -> str:
        if path.suffix == ".png":
            model, gender, n = path.stem.rsplit("-", 2)
            return f"{models[model]}-{gender}-{n}.png".replace(" ", "")
        info = parse(path)
        who = models.get(info["model"], info["model"])
        return f"{info['phase']}-{who}-{info['gender']}-{services[info['service']]}-{info['line']}.mp4".replace(" ", "")

    def media(path: Path) -> str:
        link = linked / blind_name(path)
        link.symlink_to(Path("..") / ".." / path.relative_to(run))
        src = html.escape(f"media/{link.name}")
        if path.suffix == ".png":
            return f'<img src="{src}" loading="lazy">'
        s = scores.get(str(path.relative_to(run)))
        note = (f'<small>face {s["faces"]}/{s["frames"]} · to portrait {s["to_portrait_mean"]} '
                f'(min {s["to_portrait_min"]})</small>') if s else ""
        return f'<video src="{src}" controls preload="metadata"></video>{note}'

    parts = ["<h2>Portraits</h2>"]
    picks = store.picks()
    for model in sorted(plan.data["image_models"], key=lambda m: models[m]):
        for gender in GENDERS:
            cells = []
            for path in sorted(run.glob(f"portraits/{model}-{gender}-*.png")):
                picked = picks.get(f"{model}-{gender}") == int(path.stem.rsplit("-", 1)[1])
                cells.append(f'<figure class="{"picked" if picked else ""}">{media(path)}'
                             f"<figcaption>{path.stem.rsplit('-', 1)[1]}{' · picked' if picked else ''}</figcaption></figure>")
            if cells:
                parts.append(f"<h3>{label(model, models)}, {gender}</h3><div class=row>{''.join(cells)}</div>")

    for phase in ("p2", "p3", "p4"):
        clips = sorted(run.glob(f"clips/{phase}/*.mp4"))
        if not clips:
            continue
        parts.append(f"<h2>Clips, phase {phase[1]}</h2>")
        rows: dict[tuple[str, str], list[Path]] = {}
        for clip in clips:
            info = parse(clip)
            rows.setdefault((info["model"], info["gender"]), []).append(clip)
        for (model, gender), group in sorted(rows.items(), key=lambda kv: (models.get(kv[0][0], kv[0][0]), kv[0][1])):
            cells = []
            for clip in sorted(group, key=lambda c: (services.get(parse(c)["service"], ""), parse(c)["line"])):
                info = parse(clip)
                cells.append(f"<figure>{media(clip)}<figcaption>{label(info['service'], services)} · "
                             f"{info['line']}</figcaption></figure>")
            who = label(model, models) if model in models else model
            parts.append(f"<h3>{who}, {gender}</h3><div class=row>{''.join(cells)}</div>")

    page = out / ("index-revealed.html" if reveal else "index.html")
    page.write_text(f"""<!doctype html><meta charset="utf-8"><title>Anchor experiment: {html.escape(str(plan.data['run']))}</title>
<style>
body {{ font: 15px/1.4 system-ui, sans-serif; margin: 24px; background: #111; color: #ddd; }}
.row {{ display: flex; flex-wrap: wrap; gap: 14px; }}
figure {{ margin: 0; width: 420px; }} figure.picked img {{ outline: 4px solid #f5a524; }}
img, video {{ width: 100%; border-radius: 6px; background: #000; }}
small, figcaption {{ display: block; color: #999; }}
</style>
<h1>Anchor experiment {html.escape(str(plan.data['run']))}{' (names shown)' if reveal else ' (blind)'}</h1>
{''.join(parts)}
""")
    return page
