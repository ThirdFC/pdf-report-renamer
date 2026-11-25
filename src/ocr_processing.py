"""Selected-area image processing and OCR preferences. AGPL-3.0-or-later."""
import json
import os
import tempfile
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path

import cv2
import numpy as np

MODELS = {'v6': '新版模型 PP-OCRv6', 'v4': '原有模型 PP-OCRv4', 'compare': '双模型对比'}
ENHANCEMENTS = {'original': '高清原图', 'contrast': '增强对比度', 'binary': '黑白扫描增强'}


@dataclass(frozen=True)
class OcrOptions:
    model: str = 'v6'
    dpi: int = 450
    enhancement: str = 'original'
    deskew: bool = True
    drop_leading_symbol: bool = False

    def __post_init__(self):
        if (self.model not in MODELS or self.dpi not in (300,450,600)
                or self.enhancement not in ENHANCEMENTS or not isinstance(self.deskew, bool)
                or not isinstance(self.drop_leading_symbol, bool)):
            raise ValueError('OCR 设置无效')


def filter_leading_symbol(text, enabled):
    if enabled and text and unicodedata.category(text[0])[0] in ('P','S'):
        return text[1:]
    return text


class OcrOptionsStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            return OcrOptions(**json.loads(self.path.read_text(encoding='utf-8')))
        except (OSError, ValueError, TypeError):
            return OcrOptions()

    def save(self, options):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent,
                                             prefix='ocr_', suffix='.tmp', delete=False) as stream:
                temp = Path(stream.name)
                json.dump(asdict(options), stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            if temp is not None and temp.exists():
                temp.unlink()


def estimate_skew(gray):
    # Measure elongated text-line components; ignore vertical marks and large
    # rotations so that page orientation is never guessed from a lone glyph.
    ratio = min(1., 1500/max(gray.shape))
    small = cv2.resize(gray, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA) if ratio < 1 else gray
    _, ink = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if np.mean(ink > 0) > .5:
        return 0.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, small.shape[1]//45), 3))
    lines = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    angles = []
    for contour in contours:
        rect = cv2.minAreaRect(contour)
        width, height = rect[1]
        if min(width,height) < 4 or max(width,height) < 60 or max(width,height)/min(width,height) < 3:
            continue
        points = cv2.boxPoints(rect)
        edges = np.roll(points,-1,axis=0)-points
        edge = edges[np.argmax(np.sum(edges**2, axis=1))]
        angle = float(np.degrees(np.arctan2(edge[1], edge[0])))
        angle = (angle+90) % 180 - 90
        if abs(angle) <= 10:
            angles.append(angle)
    if not angles:
        return 0.
    angle = float(np.median(angles))
    return angle if abs(angle) >= .3 else 0.


def prepare_crop(rgb, enhancement='original', deskew=True):
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    angle = estimate_skew(gray) if deskew else 0.
    if enhancement == 'original':
        image = rgb.copy()
    else:
        gray = cv2.createCLAHE(clipLimit=2., tileGridSize=(8,8)).apply(gray)
        if enhancement == 'binary':
            gray = cv2.adaptiveThreshold(gray,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                         cv2.THRESH_BINARY,51,13)
        image = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    if angle:
        h, w = image.shape[:2]
        matrix = cv2.getRotationMatrix2D((w/2,h/2),angle,1.)
        new_w = int(np.ceil(h*abs(matrix[0,1])+w*abs(matrix[0,0])))
        new_h = int(np.ceil(h*abs(matrix[0,0])+w*abs(matrix[0,1])))
        matrix[0,2] += (new_w-w)/2
        matrix[1,2] += (new_h-h)/2
        image = cv2.warpAffine(image,matrix,(new_w,new_h),flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_CONSTANT,borderValue=(255,255,255))
    # White margins help text detection when a selection closely hugs letters.
    return cv2.copyMakeBorder(image,16,16,16,16,cv2.BORDER_CONSTANT,value=(255,255,255)), angle
