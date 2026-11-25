"""Offline PDF rendering, OCR and non-overwriting renames. AGPL-3.0-or-later."""
from __future__ import annotations
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import fitz
import numpy as np
from ocr_processing import OcrOptions, prepare_crop


def scan_pdfs(folder: Path, recursive=True):
    found, errors = [], []
    def onerror(exc):
        errors.append(str(exc))
    for root, dirs, files in os.walk(folder, onerror=onerror, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(root) / d).is_symlink()
                         and not os.path.isjunction(Path(root) / d))
        for name in files:
            path = Path(root) / name
            if path.suffix.lower() == '.pdf' and not path.is_symlink():
                found.append(path)
        if not recursive:
            break
    return sorted(found, key=lambda p: str(p).casefold()), errors


def remove_whitespace(text: str) -> str:
    return re.sub(r'\s+', '', text)


def clean_name(text: str) -> str:
    text = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip(' .')
    if text.lower().endswith('.pdf'):
        text = text[:-4].rstrip(' .')
    # Keep comfortably below Windows' 255 UTF-16 code-unit component limit.
    while len(text.encode('utf-16-le')) > 360:
        text = text[:-1]
    text = text.rstrip(' .')
    if text and re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?', text, re.I):
        text = '_' + text
    return text


def open_pdf(path):
    doc = fitz.open(path)
    if doc.needs_pass:
        doc.close()
        raise ValueError('此 PDF 需要密码，请先用 PDF 软件另存为无密码文件。')
    if not len(doc):
        doc.close()
        raise ValueError('此 PDF 没有页面。')
    return doc


def render_first(path, high_resolution=False):
    with open_pdf(path) as doc:
        page = doc[0]
        rect = page.rect
        if rect.width <= 0 or rect.height <= 0:
            raise ValueError('PDF 页面尺寸无效。')
        limit = 5000 if high_resolution else 2400
        area = 16_000_000 if high_resolution else 5_000_000
        scale = min(300 / 72 if high_resolution else 160 / 72,
                    limit / max(rect.width, rect.height),
                    math.sqrt(area / (rect.width * rect.height)))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
        return pix, len(doc)


def crop_first(path, region, dpi=450):
    x0, y0, x1, y1 = region
    x0, x1 = sorted((max(0., min(1., x0)), max(0., min(1., x1))))
    y0, y1 = sorted((max(0., min(1., y0)), max(0., min(1., y1))))
    if dpi not in (300,450,600):
        raise ValueError('不支持的识别分辨率')
    with open_pdf(path) as doc:
        page = doc[0]
        rect = page.rect
        clip = fitz.Rect(rect.x0+x0*rect.width,rect.y0+y0*rect.height,
                         rect.x0+x1*rect.width,rect.y0+y1*rect.height)
        if clip.is_empty:
            raise ValueError('框选区域太小，请重新框选。')
        scale = min(dpi/72, 6000/max(clip.width,clip.height),
                    math.sqrt(12_000_000/(clip.width*clip.height)))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale,scale), clip=clip,
                              colorspace=fitz.csRGB, alpha=False)
        crop = np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,3).copy()
    if crop.shape[0] < 4 or crop.shape[1] < 4:
        raise ValueError('框选区域太小，请重新框选。')
    return crop


class OcrEngine:
    def __init__(self):
        self.engine = None
        self.new_engine = None
        self.lock = threading.Lock()

    def recognize(self, path, region):
        crop = crop_first(path, region)
        with self.lock:
            return self._standard(crop)

    def _standard(self, crop):
        result = self._standard_output(crop)
        if not result:
            return '', 0.
        return remove_whitespace(''.join(line[1] for line in result)), sum(float(line[2]) for line in result) / len(result)

    def _standard_output(self, crop):
        if self.engine is None:
            from rapidocr_onnxruntime import RapidOCR
            self.engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1,
                                    max_side_len=4096,det_limit_side_len=960)
        result, _ = self.engine(crop[:, :, ::-1].copy())
        return result

    def _new(self, crop):
        output = self._new_output(crop)
        if not output.txts:
            return '', 0.
        return remove_whitespace(''.join(output.txts)), float(np.mean(output.scores))

    def _new_output(self, crop):
        if self.new_engine is None:
            import rapidocr
            from rapidocr.main import RapidOCR
            from rapidocr import OCRVersion, ModelType
            models = Path(rapidocr.__file__).parent/'models'
            paths = {'Det': models/'PP-OCRv6_det_small.onnx',
                     'Rec': models/'PP-OCRv6_rec_small.onnx',
                     'Cls': models/'ch_ppocr_mobile_v2.0_cls_mobile.onnx'}
            for path in paths.values():
                if not path.is_file():
                    raise FileNotFoundError(f'内置 OCR 模型缺失：{path.name}，请完整解压软件。')
            params = {'Global.log_level':'warning','Global.max_side_len':4096,
                      'EngineConfig.onnxruntime.intra_op_num_threads':2,
                      'EngineConfig.onnxruntime.inter_op_num_threads':1,
                      'Det.limit_side_len':960}
            for section, path in paths.items():
                params[section+'.model_path'] = str(path)
            for section in ('Det','Rec'):
                params[section+'.ocr_version'] = OCRVersion.PPOCRV6
                params[section+'.model_type'] = ModelType.SMALL
            self.new_engine = RapidOCR(params=params)
        return self.new_engine(crop[:, :, ::-1].copy())

    def read_lines(self, rgb, model='v6'):
        """OCR text and boxes in input-image pixels, without discarding positions."""
        with self.lock:
            if model == 'v4':
                result = self._standard_output(rgb) or []
                return [OcrLine(remove_whitespace(text),box_bounds(box),float(score)) for box,text,score in result]
            output = self._new_output(rgb)
            if not output.txts:
                return []
            return [OcrLine(remove_whitespace(text),box_bounds(box),float(score))
                    for box,text,score in zip(output.boxes,output.txts,output.scores)]

    def recognize_candidates(self, path, region, options=None):
        options = options or OcrOptions()
        crop = crop_first(path,region,options.dpi)
        crop, angle = prepare_crop(crop,options.enhancement,options.deskew)
        results = []
        models = ('v6','v4') if options.model == 'compare' else (options.model,)
        with self.lock:
            for model in models:
                try:
                    text, confidence = self._new(crop) if model == 'v6' else self._standard(crop)
                    results.append(OcrCandidate(model,text,confidence,angle))
                except Exception as exc:
                    results.append(OcrCandidate(model,'',0.,angle,str(exc)))
        if all(candidate.error for candidate in results):
            raise ValueError('；'.join(c.model+'：'+c.error for c in results))
        return results


@dataclass
class OcrLine:
    text: str
    box: tuple
    confidence: float


def box_bounds(points):
    points = np.asarray(points)
    return tuple(float(x) for x in (points[:,0].min(),points[:,1].min(),points[:,0].max(),points[:,1].max()))


@dataclass
class OcrCandidate:
    model: str
    text: str
    confidence: float
    angle: float = 0.
    error: str = ''


@dataclass
class RenameRecord:
    original: Path
    renamed: Path
    signature: tuple


def signature(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


def move_without_replace(source: Path, target: Path):
    if target.exists():
        raise FileExistsError(f'已存在同名文件：{target.name}，请修改名称。')
    # On Windows os.rename is atomic and fails if the destination exists,
    # including if it appears between the pre-check and the actual rename.
    if os.name != 'nt':
        raise RuntimeError('此发行版仅支持 Windows。')
    source.rename(target)


def rename_pdf(source: Path, proposed: str):
    stem = clean_name(proposed)
    if not stem:
        raise ValueError('文件名为空，请先识别文字或输入名称。')
    target = source.with_name(stem + '.pdf')
    if str(target).casefold() == str(source).casefold():
        raise ValueError('新名称与当前名称相同。')
    before = signature(source)
    move_without_replace(source, target)
    return RenameRecord(source, target, before)


def undo_rename(record):
    if signature(record.renamed) != record.signature:
        raise ValueError('重命名后文件已被修改或替换，为避免误操作，无法撤销。')
    move_without_replace(record.renamed, record.original)
    return record.original
