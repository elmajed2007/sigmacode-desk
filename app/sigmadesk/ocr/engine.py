"""Offline Arabic OCR on ONNX Runtime (PaddleOCR PP-OCRv3 det + arabic rec, converted to ONNX).

Pipeline: DB text detection -> rotated crops -> CTC recognition (with the Arabic
"space boost") -> targeted re-recognition of merged words -> Arabic ordering and
clean-up (ocr.arabic). Matches the PaddleX 3.2 defaults the SigmaCode worker was
tuned on (limit_type=min/64, thresh 0.3, box_thresh 0.6, unclip 1.5, rec h=48).

No Paddle runtime is needed: only onnxruntime, numpy, opencv and pyclipper.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
import pyclipper

from . import arabic


@dataclass
class OcrSettings:
    min_line_score: float = 0.5
    space_boost: float = 0.02  # 0 disables
    respace_min: int = 12  # Arabic token length that triggers re-recognition (0 disables)
    reverse_mode: str = "auto"  # auto | on | off
    det_thresh: float = 0.3
    det_box_thresh: float = 0.6
    det_unclip_ratio: float = 1.5
    det_limit_side_len: int = 64
    det_max_side_limit: int = 4000
    rec_batch: int = 6


_RESPACE_FACTORS = (0.5, 0.75)


class OcrEngine:
    def __init__(self, model_dir: Path, threads: int | None = None):
        model_dir = Path(model_dir)
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        providers = ["CPUExecutionProvider"]
        self.det = ort.InferenceSession(str(model_dir / "PP-OCRv3_mobile_det.onnx"), opts, providers=providers)
        self.rec = ort.InferenceSession(str(model_dir / "arabic_PP-OCRv3_mobile_rec.onnx"), opts, providers=providers)
        chars = (model_dir / "arabic_dict.txt").read_text(encoding="utf-8").splitlines()
        chars = [c for c in chars if c != ""]
        self.characters = ["blank"] + chars + [" "]
        self.space_index = len(self.characters) - 1
        self._lock = threading.Lock()  # one image at a time per engine (predictable memory)

    # ------------------------------------------------------------------ detection
    def _det_resize(self, img, s: OcrSettings):
        h, w = img.shape[:2]
        ratio = 1.0
        if min(h, w) < s.det_limit_side_len:
            ratio = float(s.det_limit_side_len) / min(h, w)
        rh, rw = int(h * ratio), int(w * ratio)
        if max(rh, rw) > s.det_max_side_limit:
            r = float(s.det_max_side_limit) / max(rh, rw)
            rh, rw = int(rh * r), int(rw * r)
        rh = max(int(round(rh / 32) * 32), 32)
        rw = max(int(round(rw / 32) * 32), 32)
        resized = cv2.resize(img, (rw, rh))
        return resized, rh / float(h), rw / float(w)

    def detect(self, img, s: OcrSettings) -> list[np.ndarray]:
        resized, ratio_h, ratio_w = self._det_resize(img, s)
        x = resized.astype(np.float32) / 255.0
        x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        x = x.transpose(2, 0, 1)[None]
        pred = self.det.run(None, {"x": x})[0][0, 0]
        bitmap = (pred > s.det_thresh).astype(np.uint8)
        boxes = self._boxes_from_bitmap(pred, bitmap, img.shape[1], img.shape[0], s)
        return self._sort_boxes(boxes)

    def _boxes_from_bitmap(self, pred, bitmap, dest_w, dest_h, s: OcrSettings):
        height, width = bitmap.shape
        contours, _ = cv2.findContours(bitmap * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for contour in contours[:1000]:
            points, sside = self._mini_box(contour)
            if sside < 3:
                continue
            score = self._box_score(pred, points.reshape(-1, 2))
            if score < s.det_box_thresh:
                continue
            expanded = self._unclip(points, s.det_unclip_ratio)
            if expanded is None:
                continue
            box, sside = self._mini_box(expanded.reshape(-1, 1, 2))
            if sside < 3 + 2:
                continue
            box = np.array(box)
            box[:, 0] = np.clip(np.round(box[:, 0] / width * dest_w), 0, dest_w)
            box[:, 1] = np.clip(np.round(box[:, 1] / height * dest_h), 0, dest_h)
            boxes.append(box.astype(np.float32))
        return boxes

    @staticmethod
    def _mini_box(contour):
        rect = cv2.minAreaRect(contour)
        pts = sorted(list(cv2.boxPoints(rect)), key=lambda p: p[0])
        if pts[1][1] > pts[0][1]:
            i1, i4 = 0, 1
        else:
            i1, i4 = 1, 0
        if pts[3][1] > pts[2][1]:
            i2, i3 = 2, 3
        else:
            i2, i3 = 3, 2
        box = np.array([pts[i1], pts[i2], pts[i3], pts[i4]])
        return box, min(rect[1])

    @staticmethod
    def _box_score(pred, box):
        h, w = pred.shape
        xmin = int(np.clip(np.floor(box[:, 0].min()), 0, w - 1))
        xmax = int(np.clip(np.ceil(box[:, 0].max()), 0, w - 1))
        ymin = int(np.clip(np.floor(box[:, 1].min()), 0, h - 1))
        ymax = int(np.clip(np.ceil(box[:, 1].max()), 0, h - 1))
        mask = np.zeros((ymax - ymin + 1, xmax - xmin + 1), dtype=np.uint8)
        shifted = box.copy()
        shifted[:, 0] -= xmin
        shifted[:, 1] -= ymin
        cv2.fillPoly(mask, shifted.reshape(1, -1, 2).astype(np.int32), 1)
        return cv2.mean(pred[ymin:ymax + 1, xmin:xmax + 1], mask)[0]

    @staticmethod
    def _unclip(box, ratio):
        poly = box.astype(np.float64)
        area = abs(cv2.contourArea(poly.astype(np.float32)))
        length = cv2.arcLength(poly.astype(np.float32), True)
        if length == 0:
            return None
        distance = area * ratio / length
        offset = pyclipper.PyclipperOffset()
        offset.AddPath(poly.tolist(), pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
        out = offset.Execute(distance)
        if not out:
            return None
        return np.array(out[0])

    @staticmethod
    def _sort_boxes(boxes):
        boxes = sorted(boxes, key=lambda b: (b[0][1], b[0][0]))
        for i in range(len(boxes) - 1):
            for j in range(i, -1, -1):
                if abs(boxes[j + 1][0][1] - boxes[j][0][1]) < 10 and boxes[j + 1][0][0] < boxes[j][0][0]:
                    boxes[j], boxes[j + 1] = boxes[j + 1], boxes[j]
                else:
                    break
        return boxes

    @staticmethod
    def crop(img, box):
        """PaddleX get_minarea_rect_crop: min-area rectangle of the integer box, then
        a perspective crop (rotated 90 degrees when much taller than wide)."""
        rect = cv2.minAreaRect(np.array(box).astype(np.int32))
        pts = sorted(list(cv2.boxPoints(rect)), key=lambda p: p[0])
        a, d = (0, 1) if pts[1][1] > pts[0][1] else (1, 0)
        b, c = (2, 3) if pts[3][1] > pts[2][1] else (3, 2)
        pts = np.array([pts[a], pts[b], pts[c], pts[d]], dtype=np.float32)
        w = int(max(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[2] - pts[3])))
        h = int(max(np.linalg.norm(pts[0] - pts[3]), np.linalg.norm(pts[1] - pts[2])))
        if w < 1 or h < 1:
            return None
        dst = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        m = cv2.getPerspectiveTransform(pts, dst)
        out = cv2.warpPerspective(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
        if out.shape[0] / float(out.shape[1]) >= 1.5:
            out = np.rot90(out)
        return out

    # ------------------------------------------------------------------ recognition
    def recognize(self, crops: list, s: OcrSettings) -> list[tuple[str, float]]:
        results: list[tuple[str, float]] = [("", 0.0)] * len(crops)
        order = np.argsort([c.shape[1] / float(c.shape[0]) for c in crops])
        for start in range(0, len(crops), s.rec_batch):
            idx = order[start:start + s.rec_batch]
            # same resize as PaddleX OCRReisizeNormImg: each image is padded to its own
            # width (>= 320), then the batch is padded to its widest image
            sized = []
            for i in idx:
                c = crops[i]
                ratio = c.shape[1] / float(c.shape[0])
                own_w = min(int(48 * max(320 / 48.0, ratio)), 3200)
                rw = min(int(math.ceil(48 * ratio)), own_w)
                sized.append((i, own_w, max(1, rw)))
            img_w = max(w for _i, w, _r in sized)
            batch = np.zeros((len(idx), 3, 48, img_w), np.float32)
            for k, (i, _own, rw) in enumerate(sized):
                r = cv2.resize(crops[i], (rw, 48)).astype(np.float32)
                r = r.transpose(2, 0, 1) / 255.0
                r = (r - 0.5) / 0.5
                batch[k, :, :, :rw] = r
            probs = self.rec.run(None, {"x": batch})[0]
            for k, i in enumerate(idx):
                results[i] = self._ctc_decode(probs[k], s.space_boost)
        return results

    def _ctc_decode(self, prob, space_boost):
        prob = np.array(prob, dtype=np.float32, copy=True)
        if space_boost > 0:
            mask = (prob.argmax(axis=-1) == 0) & (prob[:, self.space_index] >= space_boost)
            if mask.any():
                prob[:, self.space_index] = np.where(mask, prob[:, 0] + 1e-3, prob[:, self.space_index])
        idx = prob.argmax(axis=-1)
        conf = prob.max(axis=-1)
        chars, scores, prev = [], [], -1
        for t, c in enumerate(idx):
            if c != 0 and c != prev:
                chars.append(self.characters[c])
                scores.append(conf[t])
            prev = c
        text = "".join(chars)
        return text, float(np.mean(scores)) if scores else 0.0

    # ------------------------------------------------------------------ page
    def ocr_image(self, img, s: OcrSettings | None = None, page_no: int = 1) -> dict:
        """img: BGR uint8. Returns {"page", "items": [{text, score, bbox}]} (visual order, raw)."""
        s = s or OcrSettings()
        with self._lock:
            boxes = self.detect(img, s)
            crops, kept = [], []
            for b in boxes:
                c = self.crop(img, b)
                if c is not None and c.size:
                    crops.append(c)
                    kept.append(b)
            recs = self.recognize(crops, s) if crops else []
            items = []
            for box, crop, (text, score) in zip(kept, crops, recs):
                text = text.strip()
                if s.respace_min and arabic.looks_merged(text, s.respace_min):
                    text = self._respace(self._padded_crop(img, box), text, s)
                if not text or score < s.min_line_score:
                    continue
                xs, ys = box[:, 0], box[:, 1]
                items.append({"text": text, "score": score,
                              "bbox": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]})
        return {"page": page_no, "items": items}

    @staticmethod
    def _padded_crop(img, box):
        """Axis-aligned crop with a margin of 1/8 of the line height (as the worker did)."""
        h, w = img.shape[:2]
        x1, y1 = int(box[:, 0].min()), int(box[:, 1].min())
        x2, y2 = int(box[:, 0].max()), int(box[:, 1].max())
        pad = max(2, (y2 - y1) // 8)
        return img[max(0, y1 - pad):min(h, y2 + pad), max(0, x1 - pad):min(w, x2 + pad)]

    def _respace(self, crop, text, s: OcrSettings):
        """Re-recognize at smaller sizes; accept only if letters are identical (spaces may change)."""
        best, letters = text, arabic.letters(text)
        if crop is None or crop.size == 0:
            return text
        for factor in _RESPACE_FACTORS:
            small = cv2.resize(crop, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
            if small.shape[0] < 4 or small.shape[1] < 4:
                continue
            cand, _ = self.recognize([small], s)[0]
            if arabic.letters(cand) == letters and cand.count(" ") > best.count(" "):
                best = cand
        return best

    def ocr_pages(self, images: list, s: OcrSettings | None = None, progress=None) -> dict:
        """images: list of BGR arrays (one per page). Returns arabic.build_pages(...) output."""
        s = s or OcrSettings()
        raw = []
        for n, img in enumerate(images, start=1):
            raw.append(self.ocr_image(img, s, n))
            if progress:
                progress(n, len(images))
        return arabic.build_pages(raw, s.reverse_mode)
