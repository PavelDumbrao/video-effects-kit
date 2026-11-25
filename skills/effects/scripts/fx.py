#!/usr/bin/env python3
"""Набор «видео-эффекты»: готовит проект HyperFrames из записи и ставит эффекты-сцены.

Команды: prepare, add, list, remove, build, effects. Раскладка и правила шаблонов: docs/contract.md.
Только стандартная библиотека. Для prepare нужны ffmpeg и ffprobe.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

W, H = 1080, 1920                       # канвас проекта
FPS = 30
FRAME_SIZE = (540, 960)                 # кадры для эффектов: assets/frames/f1..f6.jpg
FRAMES = [f"assets/frames/f{i}.jpg" for i in range(1, 7)]
FACE_FRAME = "frames/face.jpg"
STATE_FILE = "fx.json"
DUR_MIN, DUR_MAX = 0.8, 15.0
MIN_GAP = 1.5  # секунд чистого кадра между эффектами: меньше — предупреждение, не ошибка
GSAP_SRC = "https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js"
# Содержимое hyperframes.json — то же, что пишет `npx hyperframes init` (версия 0.9.94).
HYPERFRAMES_JSON = {
    "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
    "registry": "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
    "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"},
    "media": {"autoProxy": True},
}

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE.parent / "templates"
REGISTRY = TEMPLATES / "effects.json"
REQUIRED_TOKENS = ("__ID__", "@@DUR@@", "/*@@P@@*/{}")
TOKEN_RE = re.compile(r"__ID__|@@DUR@@|/\*@@P@@\*/\{\}|@@F([1-6])@@")
RESERVED_PARAMS = {"id", "dur", "W", "H", "face", "words"}


class FxError(Exception):
    """Ожидаемая ошибка: печатается одной строкой в stderr, код выхода 2, без трейсбека."""


def num(v: float):
    """Число для JSON и P: целые без «.0»."""
    v = float(v)
    return int(v) if v.is_integer() else round(v, 3)


def fmt(v: float) -> str:
    """Число для подстановки в HTML: до 3 знаков, без хвостовых нулей."""
    s = f"{float(v):.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def print_json(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def run(cmd: list[str], what: str) -> str:
    """Запускает внешнюю программу; при ошибке — FxError с хвостом stderr."""
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=False)
    except OSError as e:
        raise FxError(f"не удалось запустить {what}: {e}") from e
    if r.returncode != 0:
        tail = " | ".join((r.stderr or r.stdout).strip().splitlines()[-3:])
        raise FxError(f"{what} завершился с кодом {r.returncode}: {tail}")
    return r.stdout


def require_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise FxError(
            f"не найдено: {', '.join(missing)}. Установите ffmpeg (в комплекте есть ffprobe): "
            "macOS — brew install ffmpeg, Debian/Ubuntu — sudo apt install ffmpeg"
        )


def media_info(path: Path) -> dict:
    """Длительность (сек) и наличие звука по данным ffprobe."""
    out = run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        "ffprobe",
    )
    data = json.loads(out)
    streams = data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        raise FxError(f"в файле нет видеодорожки: {path}")
    dur = 0.0
    for raw in (video[0].get("duration"), data.get("format", {}).get("duration")):
        try:
            dur = float(raw)
        except (TypeError, ValueError):
            continue
        if dur > 0:
            break
    if not dur > 0:
        raise FxError(f"не удалось определить длительность: {path}")
    return {"duration": dur, "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def grab_frame(video: Path, t: float, out: Path, size) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1"]
    if size:
        cmd += ["-vf", f"scale={size[0]}:{size[1]}"]
    cmd += ["-q:v", "2", str(out)]
    run(cmd, f"ffmpeg (кадр {out.name})")
    if not out.is_file() or out.stat().st_size == 0:
        raise FxError(f"кадр не записан: {out}")


def old_effects(proj: Path) -> list[dict]:
    """Записи прошлого состояния для --force; нет или повреждено — пустой список."""
    try:
        return load_state(proj)["effects"]
    except FxError:
        return []


def cmd_prepare(a: argparse.Namespace) -> None:
    require_tools()
    src = Path(a.video).expanduser().resolve()
    if not src.is_file():
        raise FxError(f"видео не найдено: {a.video}")
    proj = Path(a.project).expanduser().resolve()
    if (proj / STATE_FILE).exists() and not a.force:
        raise FxError(f"проект уже есть: {proj}. Чтобы пересоздать его, добавьте --force")
    target = proj / "assets" / "video" / "source.mp4"
    if target.exists() and src == target.resolve():
        raise FxError("исходное видео совпадает с файлом проекта, укажите другой файл")
    info = media_info(src)

    if a.force:  # старые сцены уходят вместе с состоянием; видео и кадры перезаписываются
        for rec in old_effects(proj):
            (proj / "compositions" / f"{rec['id']}.html").unlink(missing_ok=True)
    for sub in ("assets/video", "assets/frames", "frames", "compositions"):
        (proj / sub).mkdir(parents=True, exist_ok=True)

    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(src),
           "-map", "0:v:0", "-vf", vf, "-c:v", "libx264", "-crf", "18", "-preset", "medium",
           "-pix_fmt", "yuv420p"]
    cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"] if info["has_audio"] else ["-an"]
    cmd += ["-movflags", "+faststart", str(target)]
    run(cmd, "ffmpeg (перекодирование в 1080×1920)")

    dur = round(media_info(target)["duration"], 3)
    for i, rel in enumerate(FRAMES):
        grab_frame(target, (i + 0.5) / 6 * dur, proj / rel, FRAME_SIZE)
    grab_frame(target, min(1.0, dur / 2), proj / FACE_FRAME, None)

    (proj / "hyperframes.json").write_text(
        json.dumps(HYPERFRAMES_JSON, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    st = {"version": 1, "duration": num(dur), "has_audio": info["has_audio"], "effects": []}
    write_root(proj, st)
    save_state(proj, st)  # fx.json последним: без него проект не считается готовым
    print_json({
        "project": str(proj),
        "duration": num(dur),
        "face_frame": str(proj / FACE_FRAME),
        "frames": [str(proj / p) for p in FRAMES],
    })


# ---------- состояние проекта ----------

def load_state(proj: Path) -> dict:
    path = proj / STATE_FILE
    if not path.is_file():
        raise FxError(f"в {proj} нет fx.json: сначала подготовьте проект командой prepare")
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise FxError(f"fx.json не читается ({path}): {e}") from e
    if not isinstance(st, dict) or "duration" not in st or not isinstance(st.get("effects"), list):
        raise FxError(f"fx.json повреждён: {path}")
    return st


def save_state(proj: Path, st: dict) -> None:
    (proj / STATE_FILE).write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_registry() -> dict:
    try:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise FxError(f"не найден реестр эффектов: {REGISTRY}") from e
    except json.JSONDecodeError as e:
        raise FxError(f"реестр эффектов повреждён: {REGISTRY}: {e}") from e
    for name, entry in registry.items():
        if not (isinstance(entry, dict) and isinstance(entry.get("dur"), (int, float))
                and isinstance(entry.get("about"), str) and isinstance(entry.get("params"), dict)):
            raise FxError(f"в реестре у «{name}» нужны dur (число), about (строка) и params (объект)")
        clash = RESERVED_PARAMS & set(entry["params"])
        if clash:
            raise FxError(f"в реестре у «{name}» параметры {', '.join(sorted(clash))} заняты служебными полями")
    return registry


# ---------- корень и сцены ----------

def js_literal(obj) -> str:
    """JSON для вставки в <script>: «</» экранируется, как требует контракт."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def build_root(st: dict) -> str:
    dur = fmt(st["duration"])
    media = 'playsinline data-has-audio="true"' if st["has_audio"] else "muted playsinline"
    lines = [
        "<!doctype html>",
        '<html lang="ru">',
        "<head>",
        '<meta charset="utf-8">',
        f'<script src="{GSAP_SRC}"></script>',
        "<style>body{margin:0}</style>",
        "</head>",
        "<body>",
        f'<div id="root" data-composition-id="main" data-start="0" data-duration="{dur}" '
        f'data-width="{W}" data-height="{H}" '
        f'style="position:relative;width:{W}px;height:{H}px;overflow:hidden;background:#000">',
        f'<video id="src" class="clip" src="assets/video/source.mp4" {media} data-start="0" '
        f'data-duration="{dur}" data-track-index="1" '
        f'style="position:absolute;left:0;top:0;width:{W}px;height:{H}px;object-fit:cover;z-index:1"></video>',
    ]
    for n, rec in enumerate(sorted(st["effects"], key=lambda r: r["at"])):
        rid = rec["id"]
        lines.append(
            f'<div id="{rid}" data-composition-id="{rid}" data-composition-src="compositions/{rid}.html" '
            f'data-start="{fmt(rec["at"])}" data-duration="{fmt(rec["dur"])}" '
            f'data-track-index="{2 + n}" data-track-kind="graphics" data-width="{W}" data-height="{H}" '
            f'style="z-index:{10 + n}"></div>'
        )
    lines += [
        "</div>",
        '<script>window.__timelines=window.__timelines||{};'
        'window.__timelines["main"]=gsap.timeline({paused:true});</script>',
        "</body>",
        "</html>",
    ]
    return "\n".join(lines) + "\n"


def write_root(proj: Path, st: dict) -> None:
    (proj / "index.html").write_text(build_root(st), encoding="utf-8")


def load_template(effect: str) -> str:
    path = TEMPLATES / f"{effect}.html"
    if not path.is_file():
        raise FxError(f"нет шаблона эффекта «{effect}»: {path}")
    tpl = path.read_text(encoding="utf-8")
    for tok in REQUIRED_TOKENS:
        if tok not in tpl:
            raise FxError(f"в шаблоне {path.name} нет обязательного токена {tok}")
    return tpl


def make_P(rec: dict) -> dict:
    P = {"id": rec["id"], "dur": rec["dur"], "W": W, "H": H, "face": rec["face"], "words": rec["words"]}
    P.update(rec["params"])
    P.setdefault("frames", list(FRAMES))
    return P


def scene_html(rec: dict) -> str:
    """Текст сцены: шаблон с подставленными токенами (контракт, «Каркас шаблона»)."""
    tpl = load_template(rec["effect"])
    P = make_P(rec)
    frames = P["frames"]

    def fill(m: re.Match) -> str:
        tok = m.group(0)
        if tok == "__ID__":
            return rec["id"]
        if tok == "@@DUR@@":
            return fmt(rec["dur"])
        if tok.startswith("/*"):
            return js_literal(P)
        n = int(m.group(1))
        if n > len(frames):
            raise FxError(f"в шаблоне «{rec['effect']}» нужен кадр @@F{n}@@, а кадров всего {len(frames)}")
        return frames[n - 1]

    out = TOKEN_RE.sub(fill, tpl)  # один проход: подставленные значения не трогаются повторно
    left = re.search(r"@@[A-Za-z0-9_]+@@", out)
    if left:
        raise FxError(f"в шаблоне «{rec['effect']}» неизвестный токен {left.group(0)}")
    return out


# ---------- add ----------

def parse_float(raw: str, name: str) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError) as e:
        raise FxError(f"{name}: нужно число, получено «{raw}»") from e
    if not math.isfinite(v):
        raise FxError(f"{name}: нужно конечное число")
    return v


def parse_face(raw: str) -> tuple[int, int, int, int]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4 or not all(re.fullmatch(r"-?\d+", p) for p in parts):
        raise FxError("--face: нужны четыре целых числа X,Y,W,H в пикселях кадра 1080×1920, например 370,390,330,500")
    x, y, w, h = (int(p) for p in parts)
    if w < 80 or h < 80:
        raise FxError(f"--face: рамка головы не меньше 80×80 px, получено {w}×{h}")
    if x < 0 or y < 0 or x + w > W or y + h > H:
        raise FxError(f"--face: рамка {x},{y},{w},{h} выходит за кадр {W}×{H}")
    return x, y, w, h


def face_dict(x: int, y: int, w: int, h: int) -> dict:
    return {"x": x, "y": y, "w": w, "h": h, "cx": num(x + w / 2), "cy": num(y + h / 2)}


def read_json_object(path_s: str) -> dict:
    path = Path(path_s).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise FxError(f"файл --params не найден: {path_s}") from e
    except json.JSONDecodeError as e:
        raise FxError(f"--params: неверный JSON в {path_s}: {e}") from e
    if not isinstance(data, dict):
        raise FxError("--params: нужен JSON-объект {...}")
    return data


def merge_params(base: dict, over: dict, where: str) -> None:
    for key, value in over.items():
        if key not in base:
            raise FxError(f"{where}: нет параметра «{key}». Допустимые: {', '.join(sorted(base))}")
        if isinstance(base[key], dict) and isinstance(value, dict):
            merge_params(base[key], value, f"{where}.{key}")
        else:
            base[key] = value


def parse_value(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def apply_set(params: dict, item: str) -> None:
    if "=" not in item:
        raise FxError(f"--set «{item}»: нужен вид ключ=значение, например label=Готово")
    path, raw = item.split("=", 1)
    path = path.strip()
    keys = path.split(".")
    if not all(keys):
        raise FxError(f"--set «{item}»: пустой ключ в пути")
    value = parse_value(raw)
    cur = params
    for depth, key in enumerate(keys):
        last = depth == len(keys) - 1
        if isinstance(cur, dict):
            if key not in cur:
                raise FxError(f"--set: нет параметра «{key}» (путь {path}). Допустимые: {', '.join(sorted(cur))}")
            if last:
                cur[key] = value
            else:
                cur = cur[key]
        elif isinstance(cur, list):
            if not key.isdigit() or int(key) >= len(cur):
                raise FxError(f"--set: индекс «{key}» вне списка из {len(cur)} элементов (путь {path})")
            if last:
                cur[int(key)] = value
            else:
                cur = cur[int(key)]
        else:
            raise FxError(f"--set: путь {path} глубже, чем параметр")


def check_frames(proj: Path, frames) -> None:
    if not (isinstance(frames, list) and len(frames) == 6 and all(isinstance(p, str) and p for p in frames)):
        raise FxError("frames: нужен список из шести путей к кадрам (строки)")
    for p in frames:
        if Path(p).is_absolute():
            raise FxError(f"кадр {p}: укажите путь от папки проекта, не абсолютный")
        if not (proj / p).is_file():
            raise FxError(f"кадр не найден: {p} (путь от папки проекта)")


def load_words(path: Path, at: float, dur: float) -> list[dict]:
    """Слова, начало которых попадает в окно эффекта; время — от начала эффекта."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise FxError(f"файл --words не найден: {path}") from e
    except json.JSONDecodeError as e:
        raise FxError(f"--words: неверный JSON: {e}") from e
    items = data
    if isinstance(data, dict):
        items = data.get("words")
        if items is None and isinstance(data.get("segments"), list):
            items = [w for seg in data["segments"] if isinstance(seg, dict) for w in (seg.get("words") or [])]
    if not isinstance(items, list):
        raise FxError("--words: нужен список слов или объект с ключом words / segments[].words")
    out = []
    for i, word in enumerate(items, 1):
        t = word.get("text", word.get("w")) if isinstance(word, dict) else None
        s = word.get("start", word.get("s")) if isinstance(word, dict) else None
        e = word.get("end", word.get("e")) if isinstance(word, dict) else None
        if t is None or s is None or e is None:
            raise FxError(f"--words: у слова №{i} нет текста или времени (нужны text/start/end или w/s/e)")
        try:
            start, end = float(s), float(e)
        except (TypeError, ValueError) as err:
            raise FxError(f"--words: у слова №{i} время не число") from err
        text = str(t).strip()
        if not text or not (at - 1e-6 <= start < at + dur):
            continue
        rel_start = max(0.0, start - at)
        rel_end = max(rel_start, min(end, at + dur) - at)  # конец не выходит за окно эффекта
        out.append({"w": text, "s": round(rel_start, 2), "e": round(rel_end, 2)})
    return out


def new_id(effect: str, st: dict) -> str:
    base = "fx" + re.sub(r"[^a-z0-9]", "", effect.lower())
    used = {rec["id"] for rec in st["effects"]}
    n = 1
    while f"{base}{n}" in used:
        n += 1
    return f"{base}{n}"


def cmd_add(a: argparse.Namespace) -> None:
    proj = Path(a.project).expanduser().resolve()
    st = load_state(proj)
    registry = load_registry()
    if a.effect not in registry:
        names = ", ".join(n for n in registry if not n.startswith("_"))
        raise FxError(f"неизвестный эффект «{a.effect}». Доступные: {names}")
    entry = registry[a.effect]

    x, y, w, h = parse_face(a.face)
    at = parse_float(a.at, "--at")
    dur = parse_float(a.dur, "--dur") if a.dur is not None else float(entry["dur"])
    if at < 0:
        raise FxError("--at не может быть отрицательным")
    if not DUR_MIN <= dur <= DUR_MAX:
        raise FxError(f"--dur должен быть от {DUR_MIN} до {DUR_MAX} с, получено {dur}")
    at, dur = round(at, 3), round(dur, 3)
    if at + dur > st["duration"] + 0.001:
        raise FxError(f"эффект {at}–{at + dur:.2f} с выходит за конец видео ({st['duration']} с)")
    if not a.allow_overlap:
        for other in st["effects"]:
            end = other["at"] + other["dur"]
            if at < end - 1e-9 and other["at"] < at + dur - 1e-9:
                raise FxError(f"эффект пересекается с «{other['id']}» ({other['at']:.2f}–{end:.2f} с); "
                              "сдвиньте --at или добавьте --allow-overlap")

    params = copy.deepcopy(entry["params"])
    params.setdefault("frames", list(FRAMES))
    given: set[str] = set()  # параметры верхнего уровня, которые задал человек
    if a.params:
        user = read_json_object(a.params)
        given |= set(user)
        merge_params(params, user, "--params")
    for item in a.set:
        given.add(item.split("=", 1)[0].split(".", 1)[0])
        apply_set(params, item)
    # Парные параметры (например, пункты списка и подписи к ним): сменили главный и не задали парный — парный
    # очищается, иначе в кадре останутся подписи по умолчанию к чужим пунктам.
    for main, deps in entry.get("pairs", {}).items():
        if main in given:
            for dep in deps:
                if dep not in given and dep in params:
                    params[dep] = [] if isinstance(params[dep], list) else ""
    check_frames(proj, params["frames"])

    warnings = []
    for other in st["effects"]:
        end = other["at"] + other["dur"]
        gap = at - end if at >= end - 1e-9 else other["at"] - (at + dur)
        if 0 <= gap < MIN_GAP:
            warnings.append(f"до эффекта «{other['id']}» меньше {MIN_GAP} с чистого кадра ({gap:.2f} с): "
                            "эффекты сольются, раздвиньте их или сократите --dur")

    words = load_words(Path(a.words).expanduser(), at, dur) if a.words else []
    rid = new_id(a.effect, st)
    rec = {"id": rid, "effect": a.effect, "at": num(at), "dur": num(dur),
           "face": face_dict(x, y, w, h), "words": words, "params": params}
    text = scene_html(rec)  # сначала сборка и проверка шаблона, потом запись на диск
    (proj / "compositions").mkdir(parents=True, exist_ok=True)
    (proj / "compositions" / f"{rid}.html").write_text(text, encoding="utf-8")
    st["effects"].append(rec)
    save_state(proj, st)
    write_root(proj, st)
    print_json({
        "id": rid,
        "effect": a.effect,
        "at": num(at),
        "dur": num(dur),
        "snapshot_times": [round(at + 0.25 * dur * k, 3) for k in (0.5, 1.5, 2.5, 3.5)],
        "warnings": warnings,
    })


# ---------- остальные команды ----------

def cmd_list(a: argparse.Namespace) -> None:
    proj = Path(a.project).expanduser().resolve()
    st = load_state(proj)
    print_json({
        "project": str(proj),
        "duration": st["duration"],
        "has_audio": st["has_audio"],
        "effects": sorted(st["effects"], key=lambda r: r["at"]),
    })


def cmd_remove(a: argparse.Namespace) -> None:
    proj = Path(a.project).expanduser().resolve()
    st = load_state(proj)
    rest = [rec for rec in st["effects"] if rec.get("id") != a.id]
    if len(rest) == len(st["effects"]):
        raise FxError(f"нет эффекта с id «{a.id}». Список: fx.py list --project {proj}")
    st["effects"] = rest
    (proj / "compositions" / f"{a.id}.html").unlink(missing_ok=True)
    save_state(proj, st)
    write_root(proj, st)
    print_json({"removed": a.id})


def cmd_build(a: argparse.Namespace) -> None:
    proj = Path(a.project).expanduser().resolve()
    st = load_state(proj)
    scenes = {rec["id"]: scene_html(rec) for rec in st["effects"]}  # всё собрать до записи
    (proj / "compositions").mkdir(parents=True, exist_ok=True)
    for rid, text in scenes.items():
        (proj / "compositions" / f"{rid}.html").write_text(text, encoding="utf-8")
    write_root(proj, st)
    print_json({"project": str(proj), "rebuilt": list(scenes)})


def cmd_effects(a: argparse.Namespace) -> None:
    registry = load_registry()
    print_json([
        {"name": name, "dur": entry["dur"], "about": entry["about"], "params": entry["params"]}
        for name, entry in registry.items() if not name.startswith("_")
    ])


# ---------- вход ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fx.py", description="Набор «видео-эффекты»: проект HyperFrames и эффекты-сцены.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="команда")

    def project_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--project", default="fx-project", help="папка проекта (по умолчанию fx-project)")

    sp = sub.add_parser("prepare", help="подготовить проект из записи")
    sp.add_argument("video", help="исходная запись")
    sp.add_argument("--force", action="store_true", help="пересоздать существующий проект")
    project_arg(sp)

    sp = sub.add_parser("add", help="поставить эффект на таймлайн")
    sp.add_argument("effect", help="имя из templates/effects.json")
    sp.add_argument("--face", required=True, help="рамка головы X,Y,W,H в пикселях кадра 1080×1920")
    sp.add_argument("--at", required=True, help="начало эффекта, сек")
    sp.add_argument("--dur", help="длительность, сек (по умолчанию из реестра)")
    sp.add_argument("--params", help="JSON-файл с параметрами эффекта")
    sp.add_argument("--set", action="append", default=[], metavar="КЛЮЧ=ЗНАЧЕНИЕ",
                    help="параметр, можно несколько раз (например items.2=Готово)")
    sp.add_argument("--words", help="JSON-файл со словами записи")
    sp.add_argument("--allow-overlap", action="store_true", help="разрешить пересечение по времени")
    project_arg(sp)

    sp = sub.add_parser("list", help="эффекты проекта")
    project_arg(sp)
    sp = sub.add_parser("remove", help="убрать эффект по id")
    sp.add_argument("id")
    project_arg(sp)
    sp = sub.add_parser("build", help="пересобрать index.html и сцены из fx.json")
    project_arg(sp)
    sp = sub.add_parser("effects", help="реестр эффектов")
    project_arg(sp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "prepare": cmd_prepare,
        "add": cmd_add,
        "list": cmd_list,
        "remove": cmd_remove,
        "build": cmd_build,
        "effects": cmd_effects,
    }
    try:
        handlers[args.cmd](args)
    except FxError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Прервано.", file=sys.stderr)
        return 130
    except Exception as e:  # без трейсбека: одна строка по-русски, как требует ТЗ
        print(f"Ошибка: непредвиденная ситуация ({type(e).__name__}): {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
