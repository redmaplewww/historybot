"""Render three source-grounded database excerpts without modifying the database."""
import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts' / 'knowledge_graphs'
OUT.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(f'{(ROOT / "data/three_kingdoms_rebuilt_v2.db").as_uri()}?mode=ro', uri=True)
db.execute('BEGIN')
def read(table):
    return {r[0]: json.loads(r[1]) for r in db.execute(f'SELECT id,payload FROM {table} ORDER BY id')}
claims, events, evidence, passages, sources = [read(t) for t in ('claims','events','evidence','passages','sources')]
db.close()
STAMP = datetime.now(UTC).strftime('%Y-%m-%d %H:%M')
BG, INK, MUTED = '#F4F6FA', '#182B45', '#66768A'
COLORS = {'person':'#266E91','object':'#8161A6','event':'#BA732B','claim':'#BC7536','evidence':'#25857D','source':'#52648D'}
def font(size, bold=False):
    return ImageFont.truetype('C:/Windows/Fonts/msyhbd.ttc' if bold else 'C:/Windows/Fonts/msyh.ttc', size)
def wrap(text, width, f):
    lines, line = [], ''
    for char in text:
        if char == '\n' or (line and f.getlength(line + char) > width):
            lines.append(line); line = '' if char == '\n' else char
        else: line += char
    if line: lines.append(line)
    return lines
def base(number, title, subtitle, note):
    im = Image.new('RGB', (1680, 1100), BG); d = ImageDraw.Draw(im)
    d.text((64,36), f'史证图谱 / DATABASE EXCERPT  {number}', font=font(21,True), fill=COLORS['person'])
    d.text((60,80), title, font=font(43,True), fill=INK)
    d.text((64,151), subtitle, font=font(23), fill=MUTED)
    d.line((64,196,1616,196),fill='#DBE1EA',width=2)
    d.line((64,1000,1616,1000),fill='#DBE1EA',width=2)
    for i,line in enumerate(wrap(note,1540,font(21))):
        d.text((64,1016+i*30),line,font=font(21),fill=MUTED)
    return im,d
def box(d,center,text,kind='person',w=210,h=84,size=25):
    x,y=center; color=COLORS[kind]
    d.rounded_rectangle((x-w/2,y-h/2,x+w/2,y+h/2),radius=17,fill='white',outline=color,width=3)
    lines=wrap(text,w-28,font(size,True)); yy=y-len(lines)*(size+7)/2
    for line in lines:
        d.text((x-font(size,True).getlength(line)/2,yy),line,font=font(size,True),fill=color); yy+=size+7
def edge(d,a,b,label='',color='#9CAABA',dashed=False,label_offset=(0,0),arrow=True):
    dx,dy=b[0]-a[0],b[1]-a[1]; length=math.hypot(dx,dy)
    if dashed:
        for t in range(0,int(length),19):
            end=min(t+10,length)
            d.line((a[0]+dx*t/length,a[1]+dy*t/length,a[0]+dx*end/length,a[1]+dy*end/length),fill=color,width=3)
    else: d.line((*a,*b),fill=color,width=3)
    if arrow:
        ux,uy=dx/length,dy/length
        d.polygon([b,(b[0]-15*ux+6*uy,b[1]-15*uy-6*ux),(b[0]-15*ux-6*uy,b[1]-15*uy+6*ux)],fill=color)
    if label:
        x=(a[0]+b[0])/2+label_offset[0]; y=(a[1]+b[1])/2+label_offset[1]
        tw=font(21).getlength(label)
        d.rounded_rectangle((x-tw/2-10,y-17,x+tw/2+10,y+20),radius=8,fill=BG)
        d.text((x-tw/2,y-15),label,font=font(21),fill=INK)
def source_names(items):
    ids={passages[p]['source_id'] for c in items for p in c['passage_ids']}
    return '；'.join(sources[s]['title'] for s in sorted(ids))
manifest={'snapshot_at':STAMP,'database':'data/three_kingdoms_rebuilt_v2.db','figures':[]}

# Exact SPO values are retained, including distinct surface names.
triples=[('曹操','厚待','关公'),('曹操','赠赐','赤兔马'),('曹操','赠送','纱锦髯囊'),('关公','官职','偏将军'),('关公','进入','下邳'),('关公','重视','刘皇叔'),('关公','表达忠义','皇叔')]
selected=[next(c for c in claims.values() if (c['subject'],c['predicate'],c['object'])==t) for t in triples]
assert all(c['status']=='extracted' for c in selected)
im,d=base('01','曹操与关公 · 候选关系图','7 条数据库主张 / 虚线箭头＝已抽取、待审查 / 保留原始称谓',
          '来源：'+source_names(selected)+'。文学叙述不代表已采信史实；“刘皇叔 / 皇叔”尚未在本图合并。')
pos={'曹操':(420,580),'关公':(1010,580),'赤兔马':(230,300),'纱锦髯囊':(230,875),'偏将军':(1300,290),'下邳':(1440,580),'刘皇叔':(1300,875),'皇叔':(870,875)}
for c in selected:
    a,b=pos[c['subject']],pos[c['object']]
    dx,dy=b[0]-a[0],b[1]-a[1]; scale=max(abs(dx)/115,abs(dy)/52)
    aa=(a[0]+dx/scale,a[1]+dy/scale); bb=(b[0]-dx/scale,b[1]-dy/scale)
    edge(d,aa,bb,c['predicate'],COLORS['claim'],True)
for label,p in pos.items(): box(d,p,label,'object' if label in ('赤兔马','纱锦髯囊','偏将军','下邳') else 'person')
im.save(OUT/'01_relations.png')
manifest['figures'].append({'file':'01_relations.png','claim_ids':[c['id'] for c in selected]})

chosen=[]
for name in ('司徒靖立刘永为鲁王','司徒靖立刘理为梁王','黄皓谮构刘永于后主，后主逐渐疏远刘永'):
    chosen.append(next(e for e in events.values() if e['label']==name))
im,d=base('02','刘永与刘理 · 事件参与图','蓝色＝人物原称 / 橙色＝事件 / 连线＝数据库记录的参与者',
          '展示 3 条事件候选；共同参与不等于亲属或联盟。原始时间“六月”缺少年份，未补写公历时间。')
person_pos={'司徒靖':(280,360),'刘永':(280,700),'刘理':(1400,360),'黄皓':(1400,700),'后主':(1070,915)}
event_pos=[(690,330),(1070,485),(770,735)]
for e,p in zip(chosen,event_pos):
    for name in dict.fromkeys(e['participant_surfaces']):
        a=person_pos[name]; edge(d,a,p,arrow=False)
for name,p in person_pos.items(): box(d,p,name)
for e,p in zip(chosen,event_pos):
    box(d,p,e['label'],'event',w=380,h=140,size=23)
    d.rectangle((p[0]-180,p[1]+77,p[0]+50,p[1]+108),fill=BG)
    d.text((p[0]-175,p[1]+80),'时间：'+str(e.get('date_start') or '未注明'),font=font(19),fill=MUTED)
im.save(OUT/'02_events.png')
manifest['figures'].append({'file':'02_events.png','event_ids':[e['id'] for e in chosen]})

c=next(c for c in claims.values() if c['subject']=='后主敬哀皇后' and c['object']=='张飞')
ev=[e for e in evidence.values() if e['claim_id']==c['id']]
assert ev
e=ev[0]; p=passages[e['passage_id']]; s=sources[p['source_id']]
im,d=base('03','从人物关系追溯原文 · 证据链','实体 → 候选主张 → 证据记录 → 原文段落 → 史料来源',
          '本图沿现有 ID 引用绘制；supports 表示证据记录的支持方向，Claim 当前仍为 extracted（待审查）。')
box(d,(250,305),c['subject'],w=310)
box(d,(1370,305),c['object'])
edge(d,(415,305),(730,305),'主语',COLORS['claim'],True)
box(d,(850,305),c['predicate'],'claim',w=210)
edge(d,(960,305),(1255,305),'宾语',COLORS['claim'],True)
edge(d,(850,353),(850,407),'对应主张')
box(d,(850,475),'主张 · 待审查\n'+c['statement'],'claim',w=1170,h=130,size=25)
edge(d,(850,544),(850,600),'引用证据')
quote=e.get('quote') or p['text']
if len(quote)>120: quote=quote[:120]+'…'
box(d,(850,690),'证据 · '+e['polarity']+'\n'+quote,'evidence',w=1370,h=165,size=23)
edge(d,(620,775),(445,835),'定位原文')
edge(d,(750,887),(1035,887),'所属来源')
box(d,(445,890),'原文段落\n'+p['id'],'evidence',w=550,h=104,size=22)
box(d,(1290,890),s['title']+'\n来源类型：'+s.get('kind','unknown'),'source',w=500,h=115,size=22)
im.save(OUT/'03_provenance.png')
manifest['figures'].append({'file':'03_provenance.png','claim_ids':[c['id']],'evidence_ids':[e['id']],'passage_ids':[p['id']],'source_ids':[s['id']]})
(OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(manifest,ensure_ascii=True,indent=2))
