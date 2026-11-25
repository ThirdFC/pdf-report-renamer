"""Synthetic image-only reports with shifting identifiers; no real user files."""
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
import fitz
import io

def make_samples(folder):
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    font = lambda size:ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',size)
    variants = [('01-位置正常.pdf','009',0,0,0,False),
                ('02-位置偏移.pdf','010',45,75,0,False),
                ('03-轻微倾斜.pdf','011',-20,30,2.5,False),
                ('04-交叉不一致.pdf','012',15,-15,0,True),
                ('05-候选重复.pdf','009',-30,55,0,False)]
    for name,suffix,dx,dy,angle,conflict in variants:
        image = Image.new('RGB',(1654,2339),'white')
        draw = ImageDraw.Draw(image)
        draw.text((140,140),'钢筋试验报告',fill='#182c49',font=font(65))
        draw.text((140,260),'功能测试样例 · 非真实报告',fill='#65758c',font=font(32))
        for index,label in enumerate(('报告编号','委托编号','记录编号')):
            y = 420+index*145+dy
            draw.text((720+dx,y),label,fill='black',font=font(35))
            value = '013' if conflict and index else suffix
            draw.text((950+dx,y),'AB-GJ-20250313-'+value,fill='black',font=font(34))
            draw.line((720+dx,y+65,1550+dx,y+65),fill='#8794a4',width=2)
        draw.text((140,1050),'编号位置在各文件中略有偏移，请核对候选后逐份确认。',fill='black',font=font(35))
        if angle:
            image = image.rotate(angle,resample=Image.Resampling.BICUBIC,fillcolor='white')
        stream = io.BytesIO()
        image.save(stream,format='PNG')
        with fitz.open() as doc:
            page = doc.new_page(width=595.28,height=841.89)
            page.insert_image(page.rect,stream=stream.getvalue())
            doc.new_page()
            doc.save(folder/name,deflate=True)
    return folder

if __name__ == '__main__':
    print(make_samples(Path(__file__).resolve().parent.parent/'report_samples'))
