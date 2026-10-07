#!/usr/bin/env python3
"""Regenerate the editable paper-style system figure (stdlib only)."""
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/assets/architecture.svg'
INK = '#20232b'
COLORS = {'purple':'#e9e5f4','blue':'#e2edf7','yellow':'#f8f4d8','orange':'#f9e8d4','green':'#e5efdf','grey':'#f6f7f9'}
parts = ['''<svg xmlns="http://www.w3.org/2000/svg" width="1800" height="1100" viewBox="0 0 1800 1100" role="img" aria-labelledby="title desc">
<title id="title">Reelsomet: from content plan to Android execution</title>
<desc id="desc">The Vue dashboard and optional Telegram integration control a FastAPI server. SQLite and the media library support the scheduler. A WebSocket device bridge sends commands to the Android router, Room task queue and Accessibility executor. State reports return to server logs and the dashboard.</desc>
<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#20232b"/></marker></defs>
<style>text{font-family:Arial,Helvetica,sans-serif;fill:#20232b}.title{font-family:Georgia,'Times New Roman',serif;font-size:52px}.panel{font-size:29px;font-weight:700}.name{font-size:26px;font-weight:600}.detail{font-size:20px;fill:#555c6c}.label{font-size:19px;fill:#555c6c}.small{font-size:17px;fill:#727887}</style>
<rect width="1800" height="1100" fill="white"/>''']

def text(x,y,words,kind='name',anchor='middle'):
    parts.append(f'<text x="{x}" y="{y}" class="{kind}" text-anchor="{anchor}">{escape(words)}</text>')

def rect(x,y,w,h,fill='grey',radius=14,dashed=False):
    dash=' stroke-dasharray="10 8"' if dashed else ''
    stroke='#8b909b' if dashed else INK
    parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{COLORS.get(fill,fill)}" stroke="{stroke}" stroke-width="2"{dash}/>')

def box(x,y,w,h,name,sub='',fill='blue'):
    rect(x,y,w,h,fill)
    text(x+w/2,y+h/2+(-6 if sub else 9),name)
    if sub:text(x+w/2,y+h/2+26,sub,'detail')

def line(points,dashed=False,both=False):
    path=' '.join(('M' if i==0 else 'L')+f'{p[0]} {p[1]}' for i,p in enumerate(points))
    ds=' stroke-dasharray="7 6"' if dashed else ''
    start=' marker-start="url(#arrow)"' if both else ''
    parts.append(f'<path d="{path}" fill="none" stroke="{INK}" stroke-width="2.4" stroke-linejoin="round" marker-end="url(#arrow)"{ds}{start}/>')

text(62,50,'SYSTEM OVERVIEW  /  OPEN SOURCE','small','start')
text(60,113,'Reelsomet','title','start')
text(1780,108,'Plan → Queue → Execute → Observe','detail','end')
parts.append('<path d="M60 139 H1740" stroke="#d7dbe1"/>')
rect(60,178,970,772,'grey',28,True)
rect(1090,178,650,772,'grey',28,True)
text(88,220,'(a)  Server & control plane','panel','start')
text(1118,220,'(b)  Android device','panel','start')
# Interfaces and API
box(98,268,274,93,'Vue dashboard','operator interface','purple')
box(444,268,240,93,'FastAPI','REST + admin WS','yellow')
box(754,268,238,93,'Telegram','optional interface','orange')
line([(372,315),(444,315)],both=True)
line([(684,315),(754,315)],both=True)
line([(564,361),(564,412),(259,412),(259,456)],both=True)
line([(564,412),(586,412),(586,456)],both=True)
line([(844,412),(564,412)])
box(124,456,270,123,'Media library','files + FFmpeg','blue')
box(464,456,244,123,'SQLite','accounts · jobs · logs','blue')
# Scheduler detail
rect(754,399,238,295,'blue')
text(873,440,'Scheduler')
text(873,470,'per-account policy','detail')
for i,(label,color) in enumerate([('A', '#f2c9ca'),('B','#cee1c0'),('C','#c7dbef')]):
    y=500+57*i
    parts.append(f'<rect x="774" y="{y}" width="198" height="42" rx="5" fill="white" stroke="#8b909b" stroke-dasharray="5 4"/>')
    text(791,y+27,label,'label')
    for j in range(4):parts.append(f'<rect x="{810+38*j}" y="{y+9}" width="31" height="24" rx="2" fill="{color}" stroke="#949ba7"/>')
line([(394,540),(427,540),(427,651),(754,651)])
line([(708,518),(754,518)],both=True)
text(576,641,'prepared media','label')
line([(873,694),(873,751)])
box(747,751,251,99,'Device bridge','command IDs + acks','blue')
# Android: align top bridge horizontally through a dedicated gutter
box(1140,268,550,93,'MessageRouter','WebSocket command dispatch','purple')
line([(998,800),(1060,800),(1060,315),(1140,315)])
parts.append('<text x="0" y="0" transform="translate(1048 609) rotate(-90)" class="label" text-anchor="middle">WebSocket commands</text>')
box(1193,412,444,84,'Room task queue','persistent local jobs','yellow')
line([(1415,361),(1415,412)])
rect(1140,551,550,196,'blue')
text(1415,593,'Accessibility executor')
for i,label in enumerate(['Navigate','Compose','Publish']):
    x=1160+171*i
    box(x,622,151,59,label,fill='orange')
    if i<2:line([(x+151,650),(x+171,650)])
text(1415,722,'app-specific UI flows + state checks','detail')
line([(1415,496),(1415,551)])
box(1193,805,444,74,'StateReporter',fill='purple')
line([(1415,747),(1415,805)])
text(1415,918,'Android apps: Instagram · Pinterest · Reddit','small')
# Feedback channel with no cross-module ambiguity
box(124,759,289,91,'Logs & insights','results · device status','green')
line([(1193,848),(1056,848),(1056,904),(269,904),(269,850)])
text(738,893,'WebSocket events / status & results','label')
line([(124,804),(87,804),(87,387),(235,387),(235,361)],dashed=True)
text(144,698,'observable feedback','label','start')
text(60,1010,'Figure 1.  A self-hosted publishing loop with explicit server–device boundaries.','detail','start')
text(60,1045,'Solid arrows: data & commands. Dashed arrow: dashboard updates. Queues A–C illustrate per-account work.','small','start')
text(60,1073,'Conceptual architecture mapped to the source; optional integrations and experimental workflows are documented separately.','small','start')
parts.append('</svg>')
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text('\n'.join(parts)+'\n')
print(OUT.relative_to(ROOT))
