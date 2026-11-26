"""Scan-only report-number locating and evidence generation. AGPL-3.0-or-later."""
import datetime
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path

import cv2
import numpy as np
from core import OcrLine, crop_first, signature
from ocr_processing import prepare_crop


@dataclass(frozen=True)
class ReportTemplate:
    region: tuple = (.40,.03,.99,.42)
    anchor: str = '报告编号'
    serial_digits: int = 3
    crosscheck: bool = True

    def __post_init__(self):
        a,b,c,d = self.region
        if not (0 <= a < c <= 1 and 0 <= b < d <= 1):
            raise ValueError('搜索范围无效')
        if not isinstance(self.anchor,str) or not self.anchor.strip() or len(self.anchor)>30:
            raise ValueError('定位文字不能为空，最多 30 个字')
        if not isinstance(self.serial_digits,int) or not 1 <= self.serial_digits <= 6:
            raise ValueError('流水号位数应为 1 至 6')
        if not isinstance(self.crosscheck,bool):
            raise ValueError('核对选项无效')


class ReportTemplateStore:
    def __init__(self,path):
        self.path = Path(path)

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if 'region' in data:
                data['region'] = tuple(data['region'])
            return ReportTemplate(**data)
        except (OSError,ValueError,TypeError):
            return ReportTemplate()

    def save(self,template):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=self.path.parent,delete=False) as stream:
                temp = Path(stream.name)
                json.dump(asdict(template),stream,ensure_ascii=False,indent=2)
            os.replace(temp,self.path)
        finally:
            if temp is not None and temp.exists():
                temp.unlink()


def normalize(text):
    text = unicodedata.normalize('NFKC',text)
    text = text.translate(str.maketrans({c:'-' for c in '‐‑‒–—―−﹣'}))
    return re.sub(r'\s+','',text)


@dataclass
class ReportCandidate:
    number: str
    sources: list
    confidence: float
    box: tuple
    evidence: bytes = b''
    valid_date: bool = True


@dataclass
class ReportFinding:
    candidates: list = field(default_factory=list)
    notices: list = field(default_factory=list)
    file_signature: tuple | None = None
    fallback_evidence: bytes = b''
    error: str = ''
    raw_text: str = ''

    @property
    def number(self):
        return self.candidates[0].number if self.candidates else ''


def group_rows(lines):
    rows = []
    for line in sorted(lines,key=lambda l: ((l.box[1]+l.box[3])/2,l.box[0])):
        if not line.text:
            continue
        cy = (line.box[1]+line.box[3])/2
        height = max(1,line.box[3]-line.box[1])
        nearest = next((row for row in reversed(rows)
                        if abs(cy-row['cy']) <= .55*max(height,row['height'])),None)
        if nearest is None:
            rows.append(dict(cy=cy,height=height,lines=[line]))
        else:
            nearest['lines'].append(line)
            nearest['cy'] = sum((l.box[1]+l.box[3])/2 for l in nearest['lines'])/len(nearest['lines'])
            nearest['height'] = max(nearest['height'],height)
    for row in rows:
        row['lines'].sort(key=lambda l:l.box[0])
        row['text'] = ''.join(normalize(l.text) for l in row['lines'])
    return rows


def span_box(row,start,end,context=True):
    boxes = []
    offset = 0
    for line in row['lines']:
        text = normalize(line.text)
        limit = offset+len(text)
        if start < limit and end > offset:
            x0,y0,x1,y1 = line.box
            # Retain a little prefix for context; leave enough margin for
            # proportional Chinese glyphs and slightly imprecise OCR boxes.
            left = max(0,(start-offset)/max(1,len(text))-(.12 if context else 0))
            right = min(1,(end-offset)/max(1,len(text))+(.04 if context else 0))
            boxes.append((x0+left*(x1-x0),y0,x0+right*(x1-x0),y1))
        offset = limit
    if not boxes:
        boxes = [l.box for l in row['lines']]
    return (min(b[0] for b in boxes),min(b[1] for b in boxes),
            max(b[2] for b in boxes),max(b[3] for b in boxes))


def union_boxes(boxes):
    return (min(b[0] for b in boxes),min(b[1] for b in boxes),
            max(b[2] for b in boxes),max(b[3] for b in boxes))


def report_fields(lines,anchor):
    """Separate labels from values, then associate nearby lines within field fences.

    A label may be centered beside two value lines. Row membership alone cannot
    identify that field. Other labels fence the search both vertically and across
    columns; unknown fields act as fences too, never as report anchors.
    """
    labels,values = [],[]
    keywords = '|'.join(re.escape(k) for k in dict.fromkeys((anchor,'委托编号','记录编号')))
    label_pattern = re.compile(keywords+r'|[\u4e00-\u9fff]{1,8}(?:编号|日期|名称|单位|时间|地址|电话|地点|人员|型号|规格)')
    for row in group_rows(lines):
        matches = list(label_pattern.finditer(row['text']))
        for match in matches:
            labels.append(dict(name=match.group(),box=span_box(row,*match.span(),context=False),
                               confidence=float(np.mean([l.confidence for l in row['lines']]))))
        # Slice at exact text positions, including when OCR returns label and
        # value in one box, or splits a Chinese label across several boxes.
        offset = 0
        for line in row['lines']:
            text = normalize(line.text)
            cuts = {0,len(text)}
            intervals = []
            for match in matches:
                a,b = max(0,match.start()-offset),min(len(text),match.end()-offset)
                if a < b:
                    intervals.append((a,b))
                    cuts.update((a,b))
            cuts = sorted(cuts)
            for a,b in zip(cuts,cuts[1:]):
                if a==b or any(a>=lo and b<=hi for lo,hi in intervals):
                    continue
                part = text[a:b]
                if not part.strip(':：'):
                    continue
                x0,y0,x1,y1 = line.box
                values.append(OcrLine(part,(x0+(x1-x0)*a/max(1,len(text)),y0,
                                           x0+(x1-x0)*b/max(1,len(text)),y1),line.confidence))
            offset += len(text)
    fields = [[] for _ in labels]
    unassigned = []
    for value in values:
        vx0,vy0,vx1,vy1 = value.box
        vcy = (vy0+vy1)/2
        eligible = []
        for index,label in enumerate(labels):
            x0,y0,x1,y1 = label['box']
            height = max(1,y1-y0,vy1-vy0)
            cy = (y0+y1)/2
            top,bottom,right = cy-2.2*height,cy+2.2*height,float('inf')
            for other in labels:
                if other is label:
                    continue
                a,b,c,d = other['box']
                ocy = (b+d)/2
                same_column = min(x1,c)>max(x0,a) or abs(x0-a)<height
                if same_column:
                    if ocy < cy:
                        top = max(top,(ocy+cy)/2)
                    elif ocy > cy:
                        bottom = min(bottom,(ocy+cy)/2)
                elif a >= x1 and abs(ocy-cy)<=2.2*height:
                    right = min(right,a)
            if (top < vcy < bottom and vx0 >= x1-.3*height
                    and vx1 <= right+.3*height):
                eligible.append((abs(vcy-cy)/height,index))
        if eligible:
            eligible.sort()
            # Equally close labels are ambiguous, so retain an unanchored
            # fallback rather than silently making a batch-approvable result.
            if len(eligible)==1 or eligible[1][0]-eligible[0][0]>.15:
                fields[eligible[0][1]].append(value)
                continue
        unassigned.append(value)
    return [(label,group_rows(parts)) for label,parts in zip(labels,fields)],group_rows(unassigned)


def field_matches(rows,pattern):
    for row in rows:
        for match in pattern.finditer(row['text']):
            yield match,row,match.span()
    for first,second in zip(rows,rows[1:]):
        # Only join incomplete adjacent value rows. Joining complete IDs can
        # hide multiple candidates or fabricate a new valid-looking suffix.
        if pattern.search(first['text']) or pattern.search(second['text']):
            continue
        a = union_boxes([l.box for l in first['lines']])
        b = union_boxes([l.box for l in second['lines']])
        height = max(first['height'],second['height'])
        if second['cy']-first['cy']>2*height or min(a[2],b[2])<=max(a[0],b[0]):
            continue
        joined = dict(text=first['text']+second['text'],lines=first['lines']+second['lines'])
        boundary = len(first['text'])
        for match in pattern.finditer(joined['text']):
            if match.start()<boundary<match.end():
                yield match,joined,match.span()


def evidence_png(image,box=None):
    if box:
        x0,y0,x1,y1 = box
        height = max(10,y1-y0)
        pad = max(10,height*.35)
        image = image[max(0,int(y0-pad)):min(image.shape[0],int(y1+pad)+1),
                      max(0,int(x0-pad)):min(image.shape[1],int(x1+pad)+1)]
    if not image.size:
        return b''
    factor = min(1.,1600/max(1,image.shape[1]),650/max(1,image.shape[0]))
    if factor < 1:
        image = cv2.resize(image,None,fx=factor,fy=factor,interpolation=cv2.INTER_AREA)
    ok,encoded = cv2.imencode('.png',image[:,:,::-1])
    return encoded.tobytes() if ok else b''


def original_box(box,rgb_shape,prepared_shape,angle):
    if box is None:
        return None
    h,w = rgb_shape[:2]
    x0,y0,x1,y1 = box
    points = np.array([[x0-16,y0-16],[x1-16,y0-16],[x1-16,y1-16],[x0-16,y1-16]],dtype=np.float64)
    if angle:
        matrix = cv2.getRotationMatrix2D((w/2,h/2),angle,1.)
        matrix[0,2] += (prepared_shape[1]-32-w)/2
        matrix[1,2] += (prepared_shape[0]-32-h)/2
        inverse = cv2.invertAffineTransform(matrix)
        points = np.hstack((points,np.ones((4,1))))@inverse.T
    return (max(0,float(points[:,0].min())),max(0,float(points[:,1].min())),
            min(w,float(points[:,0].max())),min(h,float(points[:,1].max())))


def extract_report(lines_by_model,template):
    pattern = re.compile(r'(?<!\d)(\d{8})-(\d{'+str(template.serial_digits)+r'})(?!\d)')
    anchor = normalize(template.anchor)
    occurrences = []
    fallback_box = None
    raw = []
    for model,lines in lines_by_model.items():
        raw.extend(row['text'] for row in group_rows(lines))
        fields,unassigned = report_fields(lines,anchor)
        fields.append((None,unassigned))
        for field,rows in fields:
            label = field['name'] if field else '搜索范围'
            boxes = ([field['box']] if field else [])+[l.box for r in rows for l in r['lines']]
            if label == anchor and fallback_box is None:
                fallback_box = union_boxes(boxes)
            matches = field_matches(rows,pattern) if field else (
                (m,r,m.span()) for r in rows for m in pattern.finditer(r['text']))
            for match,row,span in matches:
                valid = True
                try:
                    datetime.datetime.strptime(match.group(1),'%Y%m%d')
                except ValueError:
                    valid = False
                scores = [l.confidence for l in row['lines']]+([field['confidence']] if field else [])
                box = union_boxes(boxes) if field else span_box(row,*span)
                occurrences.append((match.group(),label,model,float(np.mean(scores)),box,valid))
    primary = [o for o in occurrences if o[1] == anchor]
    ordered = primary+[o for o in occurrences if o[1] != anchor]
    candidates = {}
    for number,label,model,score,box,valid in ordered:
        source = label+' / '+model
        if number not in candidates:
            candidates[number] = ReportCandidate(number,[source],score,box,valid_date=valid)
        elif source not in candidates[number].sources:
            candidates[number].sources.append(source)
    notices = []
    primary_values = {o[0] for o in primary}
    if not primary:
        notices.append('未准确定位“'+template.anchor+'”，请人工核对')
    if len(primary_values)>1:
        notices.append('报告编号识别出多个结果')
    if len(lines_by_model)>1:
        per_model = [{o[0] for o in primary if o[2] == model} for model in lines_by_model]
        if any(values != per_model[0] for values in per_model[1:]):
            notices.append('两个模型的报告编号结果不一致')
    if not candidates:
        notices.append('未提取到符合格式的编号，可手动框选或输入名称')
    elif primary:
        chosen = primary[0][0]
        if template.crosscheck:
            auxiliary = [o for o in occurrences if o[1] in ('委托编号','记录编号') and o[1] != anchor]
            if not auxiliary:
                notices.append('未找到可交叉核对的委托编号或记录编号')
            elif any(o[0] != chosen for o in auxiliary):
                notices.append('报告编号与委托编号或记录编号不一致')
        if candidates[chosen].confidence < .9:
            notices.append('报告编号识别置信度较低')
    if any(not c.valid_date for c in candidates.values()):
        notices.append('编号中的日期无效，请检查数字')
    if len(candidates)>10:
        notices.append('候选过多，仅显示前 10 个，请缩小搜索范围')
    return ReportFinding(list(candidates.values())[:10],notices,raw_text='\n'.join(raw)),fallback_box


def analyze_report(engine,path,template,options):
    before = signature(path)
    rgb = crop_first(path,template.region,options.dpi)
    prepared,angle = prepare_crop(rgb,options.enhancement,options.deskew)
    models = ('v6','v4') if options.model == 'compare' else (options.model,)
    lines_by_model, errors = {},[]
    for model in models:
        try:
            lines_by_model[model] = engine.read_lines(prepared,model)
        except Exception as exc:
            errors.append(model+'：'+str(exc))
    if not lines_by_model:
        raise ValueError('；'.join(errors))
    finding,fallback_box = extract_report(lines_by_model,template)
    finding.notices += ['某个模型失败：'+e for e in errors]
    finding.file_signature = before
    for candidate in finding.candidates:
        candidate.evidence = evidence_png(rgb,original_box(candidate.box,rgb.shape,prepared.shape,angle))
    finding.fallback_evidence = evidence_png(rgb,original_box(fallback_box,rgb.shape,prepared.shape,angle))
    if signature(path) != before:
        raise ValueError('文件在识别过程中发生变化，请重试')
    return finding
