#!/usr/bin/env python3
"""
Live camera OCR PoC for FT-0203 display correlation.

Captures frames from a camera, runs OCR on a selected ROI, and writes
timestamped NDJSON records for correlation against USB capture logs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import sys
import time
from typing import Optional

import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import pytesseract
    from pytesseract import Output as TessOutput
except ImportError:
    pytesseract = None
    TessOutput = None


def iso_utc(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()


def default_capture_path(start_ts: float) -> str:
    stamp = dt.datetime.fromtimestamp(start_ts, tz=dt.timezone.utc).strftime(
        "%Y%m%d_%H%M%SZ"
    )
    return str(pathlib.Path(f"ft0203_camera_ocr_{stamp}.ndjson"))


def parse_roi(value: str) -> tuple[int, int, int, int]:
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ROI must be x,y,w,h")
    try:
        x, y, w, h = (int(p, 10) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ROI values must be integers") from exc
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("ROI width and height must be > 0")
    if x < 0 or y < 0:
        raise argparse.ArgumentTypeError("ROI x and y must be >= 0")
    return x, y, w, h


def parse_field_roi(value: str) -> tuple[str, tuple[int, int, int, int]]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("field ROI must be name:x,y,w,h")
    name, roi_part = value.split(":", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError("field ROI name cannot be empty")
    roi = parse_roi(roi_part)
    return name, roi


def parse_corners(value: str) -> list[tuple[float, float]]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if len(parts) != 8:
        raise argparse.ArgumentTypeError("corners must be 8 numbers: x1,y1,x2,y2,x3,y3,x4,y4")
    try:
        nums = [float(p) for p in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("corners values must be numeric") from exc
    pts = [(nums[i], nums[i + 1]) for i in range(0, 8, 2)]
    return order_corners(pts)


def parse_size(value: str) -> tuple[int, int]:
    parts = [p.strip() for p in value.lower().split("x") if p.strip()]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("size must be WxH, e.g. 640x480")
    try:
        w = int(parts[0], 10)
        h = int(parts[1], 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("size must use integers") from exc
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("size dimensions must be > 0")
    return w, h


def order_corners(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) != 4:
        raise ValueError("need exactly 4 points")
    pts = np.array(points, dtype=np.float32)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).reshape(-1)
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(diff)]
    bl = pts[np.argmax(diff)]
    return [
        (float(tl[0]), float(tl[1])),
        (float(tr[0]), float(tr[1])),
        (float(br[0]), float(br[1])),
        (float(bl[0]), float(bl[1])),
    ]


def select_four_corners(frame) -> Optional[list[tuple[float, float]]]:
    win = "Select 4 Display Corners (TL, TR, BR, BL)"
    pts: list[tuple[float, float]] = []
    canvas = frame.copy()

    def redraw() -> None:
        nonlocal canvas
        canvas = frame.copy()
        for i, (px, py) in enumerate(pts):
            cv2.circle(canvas, (int(px), int(py)), 5, (0, 200, 255), -1)
            cv2.putText(
                canvas,
                str(i + 1),
                (int(px) + 8, int(py) - 8),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )
        if len(pts) == 4:
            poly = np.array([(int(x), int(y)) for x, y in pts], dtype=np.int32)
            cv2.polylines(canvas, [poly], True, (255, 180, 0), 2)
        cv2.putText(
            canvas,
            "Click 4 corners, ENTER to accept, c to cancel, r to reset",
            (10, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

    def on_mouse(event, x, y, _flags, _param) -> None:
        if event == cv2.EVENT_LBUTTONDOWN and len(pts) < 4:
            pts.append((float(x), float(y)))
            redraw()

    redraw()
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)

    while True:
        cv2.imshow(win, canvas)
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 10) and len(pts) == 4:
            cv2.destroyWindow(win)
            return order_corners(pts)
        if key == ord("r"):
            pts.clear()
            redraw()
        if key == ord("c"):
            cv2.destroyWindow(win)
            return None


def warp_frame(frame, corners: list[tuple[float, float]], warp_size: tuple[int, int]):
    w, h = warp_size
    src = np.array(corners, dtype=np.float32)
    dst = np.array(
        [(0, 0), (w - 1, 0), (w - 1, h - 1), (0, h - 1)],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(frame, matrix, (w, h))
    return warped


def load_profile(path: str) -> dict[str, object]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as exc:
        raise ValueError(f"could not read profile: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"profile is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("profile root must be a JSON object")
    return data


def load_json_file(path: str) -> dict[str, object]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as exc:
        raise ValueError(f"could not read JSON file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON root in {path} must be an object")
    return data


def merge_profile_suggestions(
    profile_path: str,
    profile_data: dict[str, object],
    suggestions_path: str,
    create_backup: bool,
) -> dict[str, object]:
    suggestions = load_json_file(suggestions_path)
    merged = dict(profile_data)

    sugg_overrides = suggestions.get("field_overrides", {})
    if not isinstance(sugg_overrides, dict):
        raise ValueError("suggestions field_overrides must be an object")

    profile_overrides = merged.get("field_overrides", {})
    if not isinstance(profile_overrides, dict):
        profile_overrides = {}

    new_overrides: dict[str, object] = dict(profile_overrides)
    for field_name, override in sugg_overrides.items():
        if not isinstance(override, dict):
            continue
        current = new_overrides.get(field_name, {})
        if not isinstance(current, dict):
            current = {}
        merged_field = dict(current)
        merged_field.update(override)
        new_overrides[str(field_name)] = merged_field

    merged["field_overrides"] = new_overrides
    if isinstance(suggestions.get("training_summary"), dict):
        merged["training_summary"] = suggestions["training_summary"]

    if create_backup:
        stamp = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        backup_path = f"{profile_path}.bak.{stamp}"
        shutil.copy2(profile_path, backup_path)
        print(f"Profile backup written to: {backup_path}")

    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")

    return merged


def draw_raw_preview(raw_frame, corners: Optional[list[tuple[float, float]]]):
    raw_overlay = raw_frame.copy()
    cv2.putText(
        raw_overlay,
        "Raw camera view (press q=quit, c=reselect corners)",
        (10, 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (240, 240, 240),
        2,
        cv2.LINE_AA,
    )
    if corners:
        poly = np.array([(int(x), int(y)) for x, y in corners], dtype=np.int32)
        cv2.polylines(raw_overlay, [poly], True, (255, 200, 0), 2)
        for idx, (cx, cy) in enumerate(corners):
            cv2.circle(raw_overlay, (int(cx), int(cy)), 5, (0, 200, 255), -1)
            cv2.putText(
                raw_overlay,
                str(idx + 1),
                (int(cx) + 6, int(cy) - 6),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )
    return raw_overlay


def clamp_roi(roi: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    x, y, w, h = roi
    x = min(max(0, x), max(0, width - 1))
    y = min(max(0, y), max(0, height - 1))
    w = min(w, width - x)
    h = min(h, height - y)
    if w <= 0 or h <= 0:
        raise ValueError("ROI is outside frame bounds")
    return x, y, w, h


def parse_int_list(value: object, fallback: list[int]) -> list[int]:
    if isinstance(value, list):
        out: list[int] = []
        for v in value:
            out.append(int(v))
        return out if out else fallback
    if isinstance(value, str):
        out = [int(p.strip(), 10) for p in value.split(",") if p.strip()]
        return out if out else fallback
    return fallback


def parse_str_set(value: object) -> Optional[set[str]]:
    if isinstance(value, list):
        return {str(v).strip() for v in value if str(v).strip()}
    if isinstance(value, str):
        vals = {v.strip() for v in value.split(",") if v.strip()}
        return vals if vals else None
    return None


def normalize_ocr_text(text: str, whitelist: str, post_regex: str = "") -> str:
    t = (text or "").replace("\n", " ").replace("\r", " ").strip()
    while "  " in t:
        t = t.replace("  ", " ")
    if whitelist:
        allowed = set(whitelist)
        t = "".join(ch for ch in t if ch in allowed or ch.isspace())
        t = t.strip()
    if post_regex:
        import re

        m = re.search(post_regex, t)
        if m:
            return m.group(0)
        return ""
    return t


def roi_motion_score(current_gray, previous_gray) -> float:
    diff = cv2.absdiff(current_gray, previous_gray)
    return float(np.mean(diff))


def default_regex_for_field(name: str) -> str:
    n = name.lower()
    if "time" in n:
        return r"[0-9]{1,2}:[0-9]{2}"
    if "hum" in n:
        return r"[0-9]{1,3}"
    if "press" in n:
        return r"[0-9]{3,5}(\.[0-9])?"
    if "temp" in n or "dew" in n or "feel" in n:
        return r"-?[0-9]{1,2}(\.[0-9])?"
    if "wind" in n or "gust" in n or "rain" in n:
        return r"[0-9]{1,3}(\.[0-9])?"
    return r"[0-9]+"


def default_whitelist_for_field(name: str) -> str:
    n = name.lower()
    if "time" in n:
        return "0123456789:"
    if "hum" in n:
        return "0123456789"
    if "press" in n:
        return "0123456789."
    return "0123456789.-"


def build_profile_suggestions(
    training_data: dict[str, list[dict[str, object]]],
    existing_overrides: dict[str, dict[str, object]],
) -> dict[str, object]:
    out: dict[str, object] = {
        "generated_at": iso_utc(time.time()),
        "field_overrides": {},
        "training_summary": {},
    }

    for field_name, samples in training_data.items():
        regex = default_regex_for_field(field_name)
        whitelist = default_whitelist_for_field(field_name)

        variant_scores: dict[str, list[float]] = {}
        psm_scores: dict[int, list[float]] = {}
        total_candidates = 0
        valid_candidates = 0

        for sample in samples:
            cands = sample.get("candidates", [])
            if not isinstance(cands, list):
                continue
            for cand in cands:
                if not isinstance(cand, dict):
                    continue
                text = normalize_ocr_text(
                    str(cand.get("text", "")),
                    whitelist,
                    post_regex="",
                )
                if not text:
                    continue
                total_candidates += 1
                if not re.search(regex, text):
                    continue
                valid_candidates += 1

                conf = cand.get("confidence")
                try:
                    conf_val = float(conf) if conf is not None else 0.0
                except (TypeError, ValueError):
                    conf_val = 0.0
                score = conf_val + min(len(text), 12)

                variant = str(cand.get("variant", ""))
                if variant:
                    variant_scores.setdefault(variant, []).append(score)

                psm_raw = cand.get("psm")
                try:
                    psm = int(psm_raw)
                    psm_scores.setdefault(psm, []).append(score)
                except (TypeError, ValueError):
                    pass

        ranked_variants = sorted(
            (
                (variant, sum(scores) / len(scores))
                for variant, scores in variant_scores.items()
                if scores
            ),
            key=lambda x: x[1],
            reverse=True,
        )
        ranked_psm = sorted(
            (
                (psm, sum(scores) / len(scores))
                for psm, scores in psm_scores.items()
                if scores
            ),
            key=lambda x: x[1],
            reverse=True,
        )

        prev = existing_overrides.get(field_name, {})
        suggested = dict(prev)
        suggested["whitelist"] = whitelist
        suggested["post_regex"] = regex
        if ranked_variants:
            suggested["variant_list"] = [v for v, _ in ranked_variants[:3]]
        if ranked_psm:
            suggested["psm_list"] = [int(p) for p, _ in ranked_psm[:2]]
        suggested.setdefault("early_conf", 70)
        suggested.setdefault("min_conf", 10)

        out["field_overrides"][field_name] = suggested
        out["training_summary"][field_name] = {
            "samples": len(samples),
            "total_candidates": total_candidates,
            "valid_candidates": valid_candidates,
            "valid_ratio": (valid_candidates / total_candidates) if total_candidates else 0.0,
            "top_variants": [v for v, _ in ranked_variants[:3]],
            "top_psm": [int(p) for p, _ in ranked_psm[:2]],
        }

    return out


def preprocess_for_lcd(gray):
    denoised = cv2.GaussianBlur(gray, (3, 3), 0)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(denoised)
    return clahe


def build_ocr_variants(gray, scale: float, green=None):
    base = preprocess_for_lcd(gray)
    variants = {
        "gray": base,
    }

    if green is not None:
        green_blur = cv2.GaussianBlur(green, (3, 3), 0)
        green_eq = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(green_blur)
        blackhat = cv2.morphologyEx(
            green_eq,
            cv2.MORPH_BLACKHAT,
            cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
        )
        variants["green"] = green_eq
        variants["blackhat"] = blackhat

    adaptive = cv2.adaptiveThreshold(
        base,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        2,
    )
    adaptive_inv = cv2.bitwise_not(adaptive)
    otsu = cv2.threshold(base, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    otsu_inv = cv2.bitwise_not(otsu)

    variants["adaptive"] = adaptive
    variants["adaptive_inv"] = adaptive_inv
    variants["otsu"] = otsu
    variants["otsu_inv"] = otsu_inv

    if green is not None:
        g_adaptive = cv2.adaptiveThreshold(
            variants["green"],
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            2,
        )
        variants["green_adaptive"] = g_adaptive
        variants["green_adaptive_inv"] = cv2.bitwise_not(g_adaptive)
        g_otsu = cv2.threshold(
            variants["green"], 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )[1]
        variants["green_otsu"] = g_otsu
        variants["green_otsu_inv"] = cv2.bitwise_not(g_otsu)
        b_otsu = cv2.threshold(
            variants["blackhat"], 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )[1]
        variants["blackhat_otsu"] = b_otsu
        variants["blackhat_otsu_inv"] = cv2.bitwise_not(b_otsu)

    if scale > 1.0:
        for name, img in list(variants.items()):
            variants[f"{name}_{scale:.1f}x"] = cv2.resize(
                img,
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_CUBIC,
            )
    return variants


def extract_text_and_confidence(img, psm: int, oem: int, whitelist: str, min_conf: float) -> tuple[str, Optional[float]]:
    config = f"--oem {oem} --psm {psm}"
    if whitelist:
        config += f" -c tessedit_char_whitelist={whitelist}"

    data = pytesseract.image_to_data(img, output_type=TessOutput.DICT, config=config)
    words: list[str] = []
    confs: list[float] = []
    for txt, conf_raw in zip(data.get("text", []), data.get("conf", [])):
        txt = (txt or "").strip()
        if not txt:
            continue
        try:
            conf = float(conf_raw)
        except (TypeError, ValueError):
            conf = -1.0
        if conf < min_conf:
            continue
        words.append(txt)
        confs.append(conf)

    if words:
        text = " ".join(words)
        conf_avg = sum(confs) / len(confs)
        return text, conf_avg

    text = pytesseract.image_to_string(img, config=config).strip()
    return text, None


def select_best_ocr(
    roi_img,
    psm_values: list[int],
    oem: int,
    whitelist: str,
    min_conf: float,
    scale: float,
    variant_allowlist: Optional[set[str]] = None,
    early_conf: Optional[float] = None,
):
    gray = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
    green = roi_img[:, :, 1]
    variants = build_ocr_variants(gray, scale=scale, green=green)
    best_text = ""
    best_conf: Optional[float] = None
    best_score = -1.0
    best_source = ""
    candidates: list[dict[str, object]] = []

    for variant_name, variant_img in variants.items():
        base_variant_name = variant_name.split("_")[0]
        if variant_allowlist is not None and base_variant_name not in variant_allowlist and variant_name not in variant_allowlist:
            continue
        for psm in psm_values:
            text, conf = extract_text_and_confidence(
                variant_img,
                psm=psm,
                oem=oem,
                whitelist=whitelist,
                min_conf=min_conf,
            )
            text = text.strip()
            if not text:
                continue

            conf_score = conf if conf is not None else 0.0
            score = conf_score + min(len(text), 24)
            candidates.append(
                {
                    "variant": variant_name,
                    "psm": psm,
                    "text": text,
                    "confidence": conf,
                    "score": score,
                }
            )
            if score > best_score:
                best_score = score
                best_text = text
                best_conf = conf
                best_source = f"{variant_name}/psm{psm}"

            if early_conf is not None and conf is not None and conf >= early_conf:
                return best_text, best_conf, best_source, candidates

    return best_text, best_conf, best_source, candidates


def main() -> int:
    parser = argparse.ArgumentParser(description="Live camera OCR capture for FT-0203 display")
    parser.add_argument("--camera-index", type=int, default=0, help="camera index for OpenCV")
    parser.add_argument("--profile", default="", help="JSON profile path with camera/OCR defaults")
    parser.add_argument(
        "--merge-suggestions",
        default="",
        help="merge suggestion JSON into --profile before running",
    )
    parser.add_argument(
        "--merge-no-backup",
        action="store_true",
        help="disable backup file creation when merging suggestions",
    )
    parser.add_argument(
        "--corners",
        type=parse_corners,
        default=None,
        help="display corners x1,y1,x2,y2,x3,y3,x4,y4",
    )
    parser.add_argument(
        "--select-corners",
        action="store_true",
        help="interactively click 4 display corners to enable perspective warp",
    )
    parser.add_argument(
        "--warp-size",
        type=parse_size,
        default=None,
        help="warped panel size as WxH, e.g. 640x480",
    )
    parser.add_argument("--width", type=int, default=0, help="requested camera width; 0 keeps default")
    parser.add_argument("--height", type=int, default=0, help="requested camera height; 0 keeps default")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between OCR samples")
    parser.add_argument("--duration", type=float, default=0.0, help="capture duration in seconds; 0 means run until Ctrl+C or q")
    parser.add_argument("--max-samples", type=int, default=0, help="stop after N OCR samples; 0 means no sample limit")
    parser.add_argument(
        "--capture",
        default="",
        nargs="?",
        const="auto",
        help="enable OCR capture mode; optionally set NDJSON output path (default: auto timestamped filename)",
    )
    parser.add_argument("--roi", type=parse_roi, default=None, help="OCR region as x,y,w,h")
    parser.add_argument(
        "--field-roi",
        action="append",
        default=[],
        help="named OCR ROI as name:x,y,w,h (can be repeated)",
    )
    parser.add_argument("--select-roi", action="store_true", help="interactively select ROI before capture")
    parser.add_argument("--preview", action="store_true", help="show live preview window")
    parser.add_argument(
        "--dual-preview",
        action="store_true",
        help="show raw camera view and adjusted OCR view at the same time",
    )
    parser.add_argument(
        "--dump-frame",
        default="",
        help="save initial frame to image path and continue (useful for manual ROI picking)",
    )
    parser.add_argument("--only-changes", action="store_true", help="emit records only when OCR text changes")
    parser.add_argument(
        "--psm-list",
        default="6,7,11",
        help="comma-separated tesseract page segmentation modes",
    )
    parser.add_argument("--oem", type=int, default=3, help="tesseract OCR engine mode")
    parser.add_argument(
        "--whitelist",
        default="0123456789.-:%CFHhPaINOUTWm/s",
        help="tesseract whitelist characters",
    )
    parser.add_argument("--min-conf", type=float, default=15.0, help="minimum confidence (0-100) for tokens")
    parser.add_argument("--scale", type=float, default=3.0, help="upscale factor for OCR variants")
    parser.add_argument("--fast", action="store_true", help="use faster OCR path with fewer variants")
    parser.add_argument(
        "--variant-list",
        default="",
        help="comma-separated OCR variant allowlist (advanced)",
    )
    parser.add_argument(
        "--early-conf",
        type=float,
        default=80.0,
        help="stop evaluating variants for a field once confidence reaches this value",
    )
    parser.add_argument(
        "--motion-threshold",
        type=float,
        default=2.0,
        help="skip field OCR when mean ROI pixel change is below this threshold",
    )
    parser.add_argument(
        "--debug-candidates",
        action="store_true",
        help="include top OCR candidates in output records",
    )
    parser.add_argument(
        "--train-samples",
        type=int,
        default=0,
        help="collect N samples and write suggested field_overrides",
    )
    parser.add_argument(
        "--train-out",
        default="ft0203_profile_suggestions.json",
        help="output file for profile training suggestions",
    )
    parser.add_argument(
        "--train-fields",
        default="",
        help="comma-separated subset of field names to train",
    )
    parser.add_argument(
        "--auto-merge-train",
        action="store_true",
        help="after training, merge generated suggestions into --profile",
    )
    args = parser.parse_args()

    if cv2 is None:
        print("Missing dependency: opencv-python (cv2)", file=sys.stderr)
        return 2
    if pytesseract is None or TessOutput is None:
        print("Missing dependency: pytesseract", file=sys.stderr)
        return 2

    if args.profile:
        try:
            profile = load_profile(args.profile)
        except ValueError as exc:
            print(f"Invalid --profile: {exc}", file=sys.stderr)
            return 2

        if args.merge_suggestions:
            try:
                profile = merge_profile_suggestions(
                    profile_path=args.profile,
                    profile_data=profile,
                    suggestions_path=args.merge_suggestions,
                    create_backup=not args.merge_no_backup,
                )
            except ValueError as exc:
                print(f"Suggestion merge failed: {exc}", file=sys.stderr)
                return 2
            print(f"Merged suggestions from: {args.merge_suggestions}")

        if args.camera_index == 0 and "camera_index" in profile:
            args.camera_index = int(profile["camera_index"])
        if args.width == 0 and "width" in profile:
            args.width = int(profile["width"])
        if args.height == 0 and "height" in profile:
            args.height = int(profile["height"])
        if args.interval == 1.0 and "interval" in profile:
            args.interval = float(profile["interval"])
        if args.corners is None and "display_corners" in profile:
            raw = profile["display_corners"]
            if isinstance(raw, list) and len(raw) == 4:
                pts = []
                for p in raw:
                    if isinstance(p, dict):
                        pts.append((float(p["x"]), float(p["y"])))
                    elif isinstance(p, list) and len(p) == 2:
                        pts.append((float(p[0]), float(p[1])))
                if len(pts) == 4:
                    args.corners = order_corners(pts)
        if args.warp_size is None and "warp_size" in profile:
            ws = profile["warp_size"]
            if isinstance(ws, dict):
                args.warp_size = (int(ws["w"]), int(ws["h"]))
            elif isinstance(ws, list) and len(ws) == 2:
                args.warp_size = (int(ws[0]), int(ws[1]))
        if args.psm_list == "6,7,11" and "psm_list" in profile:
            psm_list = profile["psm_list"]
            if isinstance(psm_list, list):
                args.psm_list = ",".join(str(int(v)) for v in psm_list)
            else:
                args.psm_list = str(psm_list)
        if args.oem == 3 and "oem" in profile:
            args.oem = int(profile["oem"])
        if args.min_conf == 15.0 and "min_conf" in profile:
            args.min_conf = float(profile["min_conf"])
        if args.scale == 3.0 and "scale" in profile:
            args.scale = float(profile["scale"])
        if not args.fast and bool(profile.get("fast", False)):
            args.fast = True
        if args.variant_list == "" and "variant_list" in profile:
            v = profile["variant_list"]
            if isinstance(v, list):
                args.variant_list = ",".join(str(x) for x in v)
            else:
                args.variant_list = str(v)
        if args.motion_threshold == 2.0 and "motion_threshold" in profile:
            args.motion_threshold = float(profile["motion_threshold"])
        if (
            args.whitelist == "0123456789.-:%CFHhPaINOUTWm/s"
            and "whitelist" in profile
        ):
            args.whitelist = str(profile["whitelist"])
        if args.roi is None and "roi" in profile:
            r = profile["roi"]
            if isinstance(r, list) and len(r) == 4:
                args.roi = tuple(int(v) for v in r)
            elif isinstance(r, dict):
                args.roi = (
                    int(r["x"]),
                    int(r["y"]),
                    int(r["w"]),
                    int(r["h"]),
                )
        if not args.field_roi and "field_rois" in profile:
            fr = profile["field_rois"]
            if isinstance(fr, dict):
                args.field_roi = []
                for name, val in fr.items():
                    if isinstance(val, list) and len(val) == 4:
                        x, y, w, h = (int(v) for v in val)
                    elif isinstance(val, dict):
                        x, y, w, h = (
                            int(val["x"]),
                            int(val["y"]),
                            int(val["w"]),
                            int(val["h"]),
                        )
                    else:
                        continue
                    args.field_roi.append(f"{name}:{x},{y},{w},{h}")
    if args.interval <= 0:
        print("--interval must be > 0", file=sys.stderr)
        return 2
    if args.duration < 0:
        print("--duration must be >= 0", file=sys.stderr)
        return 2
    if args.max_samples < 0:
        print("--max-samples must be >= 0", file=sys.stderr)
        return 2
    if args.train_samples < 0:
        print("--train-samples must be >= 0", file=sys.stderr)
        return 2
    if not (0.0 <= args.min_conf <= 100.0):
        print("--min-conf must be in range 0-100", file=sys.stderr)
        return 2
    if args.scale <= 0:
        print("--scale must be > 0", file=sys.stderr)
        return 2
    if not (0.0 <= args.early_conf <= 100.0):
        print("--early-conf must be in range 0-100", file=sys.stderr)
        return 2
    if args.motion_threshold < 0:
        print("--motion-threshold must be >= 0", file=sys.stderr)
        return 2

    try:
        psm_values = [int(p.strip(), 10) for p in args.psm_list.split(",") if p.strip()]
    except ValueError:
        print("--psm-list must be a comma-separated list of integers", file=sys.stderr)
        return 2
    if not psm_values:
        print("--psm-list cannot be empty", file=sys.stderr)
        return 2

    variant_allowlist: Optional[set[str]] = None
    if args.variant_list.strip():
        variant_allowlist = {v.strip() for v in args.variant_list.split(",") if v.strip()}

    if args.fast:
        if args.scale == 3.0:
            args.scale = 2.0
        if len(psm_values) > 1:
            psm_values = psm_values[:1]
        if variant_allowlist is None:
            variant_allowlist = {
                "gray",
                "otsu",
                "otsu_inv",
                "green_otsu",
                "green_otsu_inv",
                "blackhat_otsu",
                "blackhat_otsu_inv",
            }

    train_enabled = args.train_samples > 0
    train_fields = {
        f.strip() for f in args.train_fields.split(",") if f.strip()
    }
    if train_enabled:
        if args.max_samples == 0:
            args.max_samples = args.train_samples
        args.only_changes = False
        args.motion_threshold = 0.0
        print(
            f"Training mode enabled: samples={args.max_samples}, output={args.train_out}"
        )

    cap = cv2.VideoCapture(args.camera_index)
    if not cap.isOpened():
        print(f"Could not open camera index {args.camera_index}", file=sys.stderr)
        return 2

    if args.width > 0:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    if args.height > 0:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)

    ok, frame = cap.read()
    if not ok or frame is None:
        cap.release()
        print("Could not read initial camera frame", file=sys.stderr)
        return 2

    if args.select_corners:
        try:
            selected_corners = select_four_corners(frame)
        except cv2.error as exc:
            cap.release()
            print(f"Corner selection failed: {exc}", file=sys.stderr)
            return 4
        if selected_corners is None:
            cap.release()
            print("Corner selection cancelled", file=sys.stderr)
            return 130
        args.corners = selected_corners
        print("Selected display corners (TL,TR,BR,BL):")
        print(json.dumps(args.corners))

    frame_h, frame_w = frame.shape[:2]
    warp_size = args.warp_size if args.warp_size is not None else (frame_w, frame_h)
    if args.corners is not None:
        try:
            frame = warp_frame(frame, args.corners, warp_size)
        except cv2.error as exc:
            cap.release()
            print(f"Warp failed: {exc}", file=sys.stderr)
            return 2
        frame_h, frame_w = frame.shape[:2]

    if args.dump_frame:
        ok_write = cv2.imwrite(args.dump_frame, frame)
        if not ok_write:
            cap.release()
            print(f"Could not write --dump-frame output: {args.dump_frame}", file=sys.stderr)
            return 2
        print(f"Wrote initial frame to: {args.dump_frame}")
        print(f"Frame size: {frame_w}x{frame_h}")

    roi = args.roi
    if args.select_roi:
        try:
            selected = cv2.selectROI("Select FT-0203 Display ROI", frame, showCrosshair=True)
            cv2.destroyWindow("Select FT-0203 Display ROI")
        except cv2.error as exc:
            cap.release()
            print("ROI selection failed due to GUI backend issue.", file=sys.stderr)
            print(f"OpenCV error: {exc}", file=sys.stderr)
            print(
                "Try one of: (1) run with QT_QPA_PLATFORM=xcb, or (2) run without --select-roi and pass --roi x,y,w,h.",
                file=sys.stderr,
            )
            return 4
        if selected[2] > 0 and selected[3] > 0:
            roi = int(selected[0]), int(selected[1]), int(selected[2]), int(selected[3])
        else:
            print("ROI selection cancelled", file=sys.stderr)
            cap.release()
            return 130

    if roi is None:
        roi = (0, 0, frame_w, frame_h)
    try:
        roi = clamp_roi(roi, frame_w, frame_h)
    except ValueError as exc:
        cap.release()
        print(str(exc), file=sys.stderr)
        return 2

    x, y, w, h = roi
    print(f"Using camera index {args.camera_index} at {frame_w}x{frame_h}")
    print(f"OCR ROI: x={x}, y={y}, w={w}, h={h}")
    if args.corners is not None:
        print(f"Perspective warp active: size={frame_w}x{frame_h}")

    field_rois: dict[str, tuple[int, int, int, int]] = {}
    field_overrides: dict[str, dict[str, object]] = {}
    if args.profile:
        raw_overrides = profile.get("field_overrides", {})
        if isinstance(raw_overrides, dict):
            for k, v in raw_overrides.items():
                if isinstance(v, dict):
                    field_overrides[str(k)] = v

    for raw in args.field_roi:
        try:
            name, f_roi = parse_field_roi(raw)
            field_rois[name] = clamp_roi(f_roi, frame_w, frame_h)
        except (argparse.ArgumentTypeError, ValueError) as exc:
            cap.release()
            print(f"Invalid --field-roi '{raw}': {exc}", file=sys.stderr)
            return 2
    if field_rois:
        print("Using named field ROIs:")
        for name, (fx, fy, fw, fh) in field_rois.items():
            print(f"  - {name}: x={fx}, y={fy}, w={fw}, h={fh}")

    if args.preview and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("No GUI display detected; disabling --preview.")
        args.preview = False

    if args.dual_preview and not args.preview:
        args.preview = True

    start_ts = time.time()
    deadline = start_ts + args.duration if args.duration > 0 else None
    capture_path = ""
    out = None

    if args.capture:
        capture_path = default_capture_path(start_ts) if args.capture == "auto" else args.capture
        out = open(capture_path, "a", encoding="utf-8")
        meta = {
            "type": "meta",
            "ts": start_ts,
            "ts_iso": iso_utc(start_ts),
            "capture_path": capture_path,
            "camera_index": args.camera_index,
            "frame_width": frame_w,
            "frame_height": frame_h,
            "fast": args.fast,
            "variant_allowlist": sorted(variant_allowlist) if variant_allowlist else [],
            "early_conf": args.early_conf,
            "display_corners": (
                [{"x": c[0], "y": c[1]} for c in args.corners] if args.corners else []
            ),
            "warp_size": {"w": frame_w, "h": frame_h},
            "roi": {"x": x, "y": y, "w": w, "h": h},
            "field_rois": {
                name: {"x": fx, "y": fy, "w": fw, "h": fh}
                for name, (fx, fy, fw, fh) in field_rois.items()
            },
            "interval_s": args.interval,
            "duration_s": args.duration,
            "max_samples": args.max_samples,
            "only_changes": args.only_changes,
            "psm_list": psm_values,
            "oem": args.oem,
            "min_conf": args.min_conf,
            "scale": args.scale,
        }
        out.write(json.dumps(meta) + "\n")
        out.flush()
        print(f"Capture mode enabled. Writing NDJSON to: {capture_path}")
    else:
        print("Capture disabled (no --capture). Printing live OCR to console only.")

    prev_text = ""
    prev_field_result: dict[str, dict[str, object]] = {}
    prev_field_gray: dict[str, object] = {}
    training_data: dict[str, list[dict[str, object]]] = {}
    samples = 0
    last_tick = 0.0
    preview_disabled_due_to_error = False

    try:
        while True:
            now = time.time()
            if deadline is not None and now >= deadline:
                break
            if args.max_samples > 0 and samples >= args.max_samples:
                break

            if now - last_tick < args.interval:
                if args.preview:
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break
                continue
            last_tick = now

            ok, frame = cap.read()
            if not ok or frame is None:
                print("Camera read failed", file=sys.stderr)
                return 3

            raw_frame = frame.copy()

            if args.corners is not None:
                try:
                    frame = warp_frame(frame, args.corners, (frame_w, frame_h))
                except cv2.error as exc:
                    print(f"Warp failed on frame: {exc}", file=sys.stderr)
                    return 3

            field_results: dict[str, dict[str, object]] = {}
            all_candidates: list[dict[str, object]] = []

            if field_rois:
                for name, (fx, fy, fw, fh) in field_rois.items():
                    f_frame = frame[fy : fy + fh, fx : fx + fw]
                    f_gray = cv2.cvtColor(f_frame, cv2.COLOR_BGR2GRAY)

                    override = field_overrides.get(name, {})
                    local_psm = parse_int_list(override.get("psm_list"), psm_values)
                    local_whitelist = str(override.get("whitelist", args.whitelist))
                    local_min_conf = float(override.get("min_conf", args.min_conf))
                    local_scale = float(override.get("scale", args.scale))
                    local_early = float(override.get("early_conf", args.early_conf))
                    local_variants = parse_str_set(override.get("variant_list"))
                    if local_variants is None:
                        local_variants = variant_allowlist
                    post_regex = str(override.get("post_regex", "")).strip()

                    motion_score = None
                    if name in prev_field_gray and args.motion_threshold > 0:
                        motion_score = roi_motion_score(f_gray, prev_field_gray[name])
                        if motion_score < args.motion_threshold and name in prev_field_result:
                            cached = dict(prev_field_result[name])
                            cached["motion_score"] = motion_score
                            cached["skipped"] = True
                            field_results[name] = cached
                            continue

                    text, conf, source, candidates = select_best_ocr(
                        f_frame,
                        psm_values=local_psm,
                        oem=args.oem,
                        whitelist=local_whitelist,
                        min_conf=local_min_conf,
                        scale=local_scale,
                        variant_allowlist=local_variants,
                        early_conf=local_early,
                    )
                    text = normalize_ocr_text(text, local_whitelist, post_regex=post_regex)

                    if train_enabled and (not train_fields or name in train_fields):
                        normalized_candidates: list[dict[str, object]] = []
                        for cand in candidates:
                            if not isinstance(cand, dict):
                                continue
                            c_text = normalize_ocr_text(
                                str(cand.get("text", "")),
                                local_whitelist,
                                post_regex="",
                            )
                            if not c_text:
                                continue
                            normalized_candidates.append(
                                {
                                    "variant": cand.get("variant", ""),
                                    "psm": cand.get("psm", ""),
                                    "text": c_text,
                                    "confidence": cand.get("confidence"),
                                    "score": cand.get("score"),
                                }
                            )
                        training_data.setdefault(name, []).append(
                            {
                                "text": text,
                                "confidence": conf,
                                "candidates": normalized_candidates,
                            }
                        )

                    field_results[name] = {
                        "text": text,
                        "confidence": conf,
                        "source": source,
                        "roi": {"x": fx, "y": fy, "w": fw, "h": fh},
                        "motion_score": motion_score,
                        "skipped": False,
                    }
                    prev_field_result[name] = dict(field_results[name])
                    prev_field_gray[name] = f_gray
                    if args.debug_candidates and candidates:
                        all_candidates.extend(
                            {
                                "field": name,
                                **c,
                            }
                            for c in candidates
                        )
                text = " | ".join(field_results[n]["text"] for n in sorted(field_results))
                conf_values = [
                    float(v["confidence"])
                    for v in field_results.values()
                    if v["confidence"] is not None
                ]
                conf = (sum(conf_values) / len(conf_values)) if conf_values else None
                source = "fields"
                candidates = []
            else:
                roi_frame = frame[y : y + h, x : x + w]
                text, conf, source, candidates = select_best_ocr(
                    roi_frame,
                    psm_values=psm_values,
                    oem=args.oem,
                    whitelist=args.whitelist,
                    min_conf=args.min_conf,
                    scale=args.scale,
                    variant_allowlist=variant_allowlist,
                    early_conf=args.early_conf,
                )

            changed = text != prev_text

            if not args.only_changes or changed:
                ts = time.time()
                rec = {
                    "type": "ocr",
                    "ts": ts,
                    "ts_iso": iso_utc(ts),
                    "sample": samples,
                    "roi": {"x": x, "y": y, "w": w, "h": h},
                    "text": text,
                    "confidence": conf,
                    "source": source,
                    "changed": changed,
                }
                if field_results:
                    rec["fields"] = field_results
                if args.debug_candidates and candidates:
                    rec["candidates"] = sorted(
                        candidates, key=lambda c: c["score"], reverse=True
                    )[:5]
                if args.debug_candidates and all_candidates:
                    rec["field_candidates"] = sorted(
                        all_candidates, key=lambda c: c["score"], reverse=True
                    )[:10]
                if out is not None:
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                print(json.dumps(rec))

            prev_text = text
            samples += 1

            if args.preview:
                try:
                    overlay = frame.copy()
                    cv2.rectangle(overlay, (x, y), (x + w, y + h), (0, 200, 255), 2)

                    for name, (fx, fy, fw, fh) in field_rois.items():
                        cv2.rectangle(overlay, (fx, fy), (fx + fw, fy + fh), (255, 180, 0), 2)
                        field_val = ""
                        if name in field_results:
                            field_val = str(field_results[name].get("text", "")).strip()
                        field_label = f"{name}={field_val}" if field_val else name
                        cv2.putText(
                            overlay,
                            field_label,
                            (fx, max(20, fy - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.45,
                            (255, 180, 0),
                            2,
                            cv2.LINE_AA,
                        )
                        cv2.putText(
                            overlay,
                            field_label,
                            (fx, max(20, fy - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.45,
                            (0, 0, 0),
                            1,
                            cv2.LINE_AA,
                        )

                    label = text if text else "<no text>"
                    if len(label) > 80:
                        label = label[:77] + "..."
                    cv2.putText(
                        overlay,
                        label,
                        (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.8,
                        (0, 255, 0),
                        2,
                        cv2.LINE_AA,
                    )

                    if args.dual_preview:
                        raw_overlay = draw_raw_preview(raw_frame, args.corners)
                        cv2.imshow("FT-0203 Camera Raw", raw_overlay)

                    cv2.imshow("FT-0203 OCR Preview (press q to quit)", overlay)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord("q"):
                        break
                    if key == ord("c") and args.dual_preview:
                        try:
                            selected_corners = select_four_corners(raw_frame)
                        except cv2.error as exc:
                            print(f"Corner reselection failed: {exc}", file=sys.stderr)
                            selected_corners = None
                        if selected_corners is not None:
                            args.corners = selected_corners
                            print("Updated display corners (TL,TR,BR,BL):")
                            print(json.dumps(args.corners))
                except cv2.error as exc:
                    if not preview_disabled_due_to_error:
                        print(f"Preview disabled due to GUI error: {exc}", file=sys.stderr)
                        preview_disabled_due_to_error = True
                    args.preview = False
    except KeyboardInterrupt:
        print("Interrupted by user.")
    finally:
        cap.release()
        cv2.destroyAllWindows()
        if out is not None:
            out.close()

    if train_enabled:
        suggestions = build_profile_suggestions(training_data, field_overrides)
        with open(args.train_out, "w", encoding="utf-8") as f:
            json.dump(suggestions, f, indent=2)
            f.write("\n")
        print(f"Training suggestions written to: {args.train_out}")
        for field_name, summary in suggestions.get("training_summary", {}).items():
            ratio = summary.get("valid_ratio", 0.0)
            top = summary.get("top_variants", [])
            print(
                f"  {field_name}: valid_ratio={ratio:.2f} top_variants={top}"
            )

        if args.auto_merge_train:
            if not args.profile:
                print("--auto-merge-train requires --profile", file=sys.stderr)
                return 2
            try:
                merge_profile_suggestions(
                    profile_path=args.profile,
                    profile_data=load_profile(args.profile),
                    suggestions_path=args.train_out,
                    create_backup=not args.merge_no_backup,
                )
            except ValueError as exc:
                print(f"Auto-merge failed: {exc}", file=sys.stderr)
                return 2
            print(f"Auto-merged training suggestions into: {args.profile}")

    print(f"OCR capture complete: samples={samples}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
