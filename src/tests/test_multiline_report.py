import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from core import OcrLine
from report_review import ReportTemplate,extract_report


def line(text,x,y,width=340,height=24):
    return OcrLine(text,(x,y,x+width,y+height),.99)


def extract(lines,**kwargs):
    return extract_report({'v6':lines},ReportTemplate(crosscheck=False,**kwargs))[0]


@pytest.mark.parametrize('label_y',[0,15,30])
@pytest.mark.parametrize('indent',[0,70])
def test_two_line_field_with_centered_or_top_aligned_label(label_y,indent):
    finding=extract([line('报告编号',20,label_y,120),
                     line('HSZQXI-ZTJDQ-J0-HJW-',170,0),
                     line('20250721-001',170+indent,34,220)])
    assert finding.number=='20250721-001'
    assert finding.candidates[0].sources==['报告编号 / v6']
    assert not finding.notices
    # Evidence contains the label and both value lines, not only the suffix.
    assert finding.candidates[0].box==(20,0,510,58)


def test_label_split_across_ocr_tokens_and_two_value_lines():
    finding=extract([line('报 告',20,15,55),line('编 号',80,15,55),
                     line('AB-CD-',170,0),line('20250721-001',170,34,220)])
    assert finding.number=='20250721-001' and not finding.notices


@pytest.mark.parametrize('first,second',[('AB-20250721-','001'),('AB-2025','0721-001'),
                                        ('AB-20250721','-001')])
def test_date_or_serial_can_wrap_between_adjacent_value_rows(first,second):
    finding=extract([line('报告编号',20,15,120),line(first,170,0),line(second,210,34,180)])
    assert finding.number=='20250721-001' and not finding.notices
    assert finding.candidates[0].box[1:4:2]==(0,58)


def test_label_and_value_in_one_ocr_box_can_continue_below():
    finding=extract([line('报告编号:AB-20250721-',20,0,460),line('001',220,34,80)])
    assert finding.number=='20250721-001' and not finding.notices


@pytest.mark.parametrize('other_label',['委托编号','记录编号','样品编号','报告日期'])
def test_never_borrow_number_from_next_field(other_label):
    finding=extract([line('报告编号',20,0,120),line('AB-CD-',170,0),
                     line(other_label,20,65,120),line('20250721-001',170,65)])
    assert any('未准确定位' in n for n in finding.notices)
    assert all('报告编号 / v6' not in c.sources for c in finding.candidates)


def test_incomplete_suffix_cannot_join_through_next_field():
    finding=extract([line('报告编号',20,0,120),line('AB-20250721-',170,0),
                     line('委托编号',20,34,120),line('001',170,34,100)])
    assert not finding.number


def test_other_column_does_not_supply_a_report_number():
    finding=extract([line('报告编号',20,10,120),line('AB-CD-',170,0,220),
                     line('委托编号',440,15,120),line('20250721-001',600,34,220)])
    assert all('报告编号 / v6' not in c.sources for c in finding.candidates)
    assert any('未准确定位' in n for n in finding.notices)


def test_two_complete_values_remain_ambiguous():
    finding=extract([line('报告编号',20,15,120),line('20250721-001',170,0),
                     line('20250721-002',170,34)])
    assert {c.number for c in finding.candidates}=={'20250721-001','20250721-002'}
    assert any('多个结果' in n for n in finding.notices)


def test_wrapped_report_still_crosschecks_auxiliary_and_models():
    lines=[line('报告编号',20,15,120),line('AB-CD-',170,0),line('20250721-001',220,34),
           line('委托编号',20,100,120),line('AB-20250721-001',170,100)]
    finding,_=extract_report({'v6':lines,'v4':lines},ReportTemplate())
    assert finding.number=='20250721-001' and not finding.notices
    assert len(finding.candidates[0].sources)==4


def test_distant_or_unaligned_fragments_are_not_joined():
    for x,y in [(170,160),(600,34)]:
        finding=extract([line('报告编号',20,15,120),line('AB-20250721-',170,0),
                         line('001',x,y,100)])
        assert not finding.number


def test_multiline_field_supports_configured_anchor_and_serial_length():
    finding=extract([line('检验号码',20,15,120),line('AB-20250721-',170,0),line('0001',210,34,180)],
                    anchor='检验号码',serial_digits=4)
    assert finding.number=='20250721-0001' and not finding.notices
