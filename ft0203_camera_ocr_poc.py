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
import sys
import time
from typing import Optional

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


def clamp_roi(roi: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    x, y, w, h = roi
    x = min(max(0, x), max(0, width - 1))
    y = min(max(0, y), max(0, height - 1))
    w = min(w, width - x)
    h = min(h, height - y)
    if w <= 0 or h <= 0:
        raise ValueError("ROI is outside frame bounds")
    return x, y, w, h


def preprocess_for_lcd(gray):
    denoised = cv2.GaussianBlur(gray, (3, 3), 0)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(denoised)
    return clahe


def build_ocr_variants(gray, scale: float):
    base = preprocess_for_lcd(gray)
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

    variants = {
        "gray": base,
        "adaptive": adaptive,
        "adaptive_inv": adaptive_inv,
        "otsu": otsu,
        "otsu_inv": otsu_inv,
    }
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
    gray,
    psm_values: list[int],
    oem: int,
    whitelist: str,
    min_conf: float,
    scale: float,
):
    variants = build_ocr_variants(gray, scale=scale)
    best_text = ""
    best_conf: Optional[float] = None
    best_score = -1.0
    best_source = ""
    candidates: list[dict[str, object]] = []

    for variant_name, variant_img in variants.items():
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

    return best_text, best_conf, best_source, candidates


def main() -> int:
    parser = argparse.ArgumentParser(description="Live camera OCR capture for FT-0203 display")
    parser.add_argument("--camera-index", type=int, default=0, help="camera index for OpenCV")
    parser.add_argument("--profile", default="", help="JSON profile path with camera/OCR defaults")
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
    parser.add_argument(
        "--debug-candidates",
        action="store_true",
        help="include top OCR candidates in output records",
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

        if args.camera_index == 0 and "camera_index" in profile:
            args.camera_index = int(profile["camera_index"])
        if args.width == 0 and "width" in profile:
            args.width = int(profile["width"])
        if args.height == 0 and "height" in profile:
            args.height = int(profile["height"])
        if args.interval == 1.0 and "interval" in profile:
            args.interval = float(profile["interval"])
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
    if not (0.0 <= args.min_conf <= 100.0):
        print("--min-conf must be in range 0-100", file=sys.stderr)
        return 2
    if args.scale <= 0:
        print("--scale must be > 0", file=sys.stderr)
        return 2

    try:
        psm_values = [int(p.strip(), 10) for p in args.psm_list.split(",") if p.strip()]
    except ValueError:
        print("--psm-list must be a comma-separated list of integers", file=sys.stderr)
        return 2
    if not psm_values:
        print("--psm-list cannot be empty", file=sys.stderr)
        return 2

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

    field_rois: dict[str, tuple[int, int, int, int]] = {}
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

            field_results: dict[str, dict[str, object]] = {}
            all_candidates: list[dict[str, object]] = []

            if field_rois:
                for name, (fx, fy, fw, fh) in field_rois.items():
                    f_frame = frame[fy : fy + fh, fx : fx + fw]
                    gray = cv2.cvtColor(f_frame, cv2.COLOR_BGR2GRAY)
                    text, conf, source, candidates = select_best_ocr(
                        gray,
                        psm_values=psm_values,
                        oem=args.oem,
                        whitelist=args.whitelist,
                        min_conf=args.min_conf,
                        scale=args.scale,
                    )
                    field_results[name] = {
                        "text": text,
                        "confidence": conf,
                        "source": source,
                        "roi": {"x": fx, "y": fy, "w": fw, "h": fh},
                    }
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
                gray = cv2.cvtColor(roi_frame, cv2.COLOR_BGR2GRAY)
                text, conf, source, candidates = select_best_ocr(
                    gray,
                    psm_values=psm_values,
                    oem=args.oem,
                    whitelist=args.whitelist,
                    min_conf=args.min_conf,
                    scale=args.scale,
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

                    panel_lines = []
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
                        if field_val:
                            panel_lines.append(f"{name}: {field_val}")

                    if panel_lines:
                        panel_x = 10
                        panel_y = 50
                        panel_h = 22 + (len(panel_lines) * 20)
                        panel_w = 320
                        cv2.rectangle(
                            overlay,
                            (panel_x, panel_y),
                            (panel_x + panel_w, panel_y + panel_h),
                            (30, 30, 30),
                            -1,
                        )
                        cv2.rectangle(
                            overlay,
                            (panel_x, panel_y),
                            (panel_x + panel_w, panel_y + panel_h),
                            (90, 180, 255),
                            1,
                        )
                        for idx, line in enumerate(panel_lines):
                            cv2.putText(
                                overlay,
                                line,
                                (panel_x + 8, panel_y + 18 + (idx * 20)),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                0.5,
                                (240, 240, 240),
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
                    cv2.imshow("FT-0203 OCR Preview (press q to quit)", overlay)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break
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

    print(f"OCR capture complete: samples={samples}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
