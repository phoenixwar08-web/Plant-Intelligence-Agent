#!/usr/bin/env python3
"""Read-only freshness and visual-history audit for soil2."""
from __future__ import annotations
import argparse, fcntl, json, os, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

BASE=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(BASE))
from analytics.trend_analyzer import DEVICE_CODE, TIMEZONE, normalize_datetime

OUTPUT=BASE/'outputs'; STATUS_PATH=OUTPUT/'soil2_status.json'; DECISION_PATH=OUTPUT/'soil2_decision.json'; TREND_PATH=OUTPUT/'soil2_trend.json'
PHASE3_PATH=Path('/root/water/phase3/soil2/system_state.json'); VISION_HISTORY=OUTPUT/'vision'/'history'/'soil2.jsonl'; IMAGE_PATH=OUTPUT/'vision'/'images'/'soil2.jpg'
REPORT_PATH=OUTPUT/'health'/'soil2_data_quality.json'; HISTORY_PATH=OUTPUT/'health'/'history'/'soil2.jsonl'
THRESHOLDS={'sensor':timedelta(minutes=15),'status':timedelta(minutes=30),'phase3':timedelta(minutes=30),'decision':timedelta(minutes=30),'trend':timedelta(minutes=10),'visual':timedelta(hours=24)}

def _load_json(path):
    try:
        v=json.loads(Path(path).read_text(encoding='utf-8')); return v if isinstance(v,dict) else None
    except (OSError,json.JSONDecodeError): return None
def _read_lines(path):
    try: return Path(path).read_text(encoding='utf-8').splitlines()
    except OSError: return []
def _image_mtime(path):
    try: return datetime.fromtimestamp(Path(path).stat().st_mtime,TIMEZONE)
    except OSError: return None
def _now(now=None): return normalize_datetime(now) or datetime.now(TIMEZONE)
def _entry(value, threshold, now):
    dt=normalize_datetime(value)
    if dt is None: return {'state':'unavailable','observed_at':None}
    return {'state':'fresh' if dt<=now and now-dt<=threshold else 'stale','observed_at':dt.isoformat()}
def _phase_time(data):
    exp=(data or {}).get('irrigation_style_experiment',{})
    if isinstance(exp,dict) and isinstance(exp.get('updated_at'),(int,float)): return datetime.fromtimestamp(exp['updated_at'],TIMEZONE)
    for key in ('updated_at','generated_at','timestamp'):
        if key in (data or {}): return data[key]
    try: return datetime.fromtimestamp(PHASE3_PATH.stat().st_mtime,TIMEZONE)
    except OSError: return None
def _vision(lines):
    valid=bad=0; latest=None
    for line in lines:
        try: r=json.loads(line); at=normalize_datetime(r.get('observed_at')) if isinstance(r,dict) else None
        except json.JSONDecodeError: r=None; at=None
        if not isinstance(r,dict) or r.get('device_code')!=DEVICE_CODE or at is None or not isinstance(r.get('visual'),dict): bad+=1; continue
        valid+=1; latest=max(latest,at) if latest else at
    return {'total_lines':len(lines),'valid_lines':valid,'malformed_lines':bad,'latest_observed_at':latest.isoformat() if latest else None}
def build_quality_report(device_code=DEVICE_CODE,now=None):
    if device_code!=DEVICE_CODE: raise ValueError('only soil2 is supported')
    now=_now(now); status=_load_json(STATUS_PATH); decision=_load_json(DECISION_PATH); trend=_load_json(TREND_PATH); phase=_load_json(PHASE3_PATH); visual=(status or {}).get('visual',{}); history=_vision(_read_lines(VISION_HISTORY))
    fresh={'sensor':_entry((status or {}).get('sensor',{}).get('recv_time'),THRESHOLDS['sensor'],now),'status':_entry((status or {}).get('generated_at'),THRESHOLDS['status'],now),'phase3':_entry(_phase_time(phase),THRESHOLDS['phase3'],now),'decision':_entry((decision or {}).get('generated_at'),THRESHOLDS['decision'],now),'trend':_entry((trend or {}).get('generated_at'),THRESHOLDS['trend'],now),'visual':_entry(visual.get('observed_at'),THRESHOLDS['visual'],now)}
    image=_image_mtime(IMAGE_PATH); fresh['visual']['image_observed_at']=image.isoformat() if image else None
    findings=[]
    for source,item in fresh.items():
        if item['state']!='fresh': findings.append({'code':f'{source}_{item["state"]}','severity':'critical' if item['state']=='unavailable' and source in ('sensor','status','phase3','decision','trend') else 'warning','source':source,'observed_at':item['observed_at'],'message':f'{source} 数据{ "不可用" if item["state"]=="unavailable" else "已过期"}'})
    if history['malformed_lines']: findings.append({'code':'vision_history_malformed','severity':'warning','source':'vision_history','observed_at':history['latest_observed_at'],'message':f'视觉历史存在 {history["malformed_lines"]} 条损坏记录'})
    core=[fresh[k]['state'] for k in ('sensor','status','phase3','decision','trend')]
    overall='unavailable' if 'unavailable' in core else 'degraded' if findings or history['valid_lines']<2 else 'healthy'
    return {'schema_version':1,'device_code':device_code,'generated_at':now.isoformat(),'timezone':'Asia/Shanghai','overall':overall,'freshness':fresh,'history_integrity':{'vision_jsonl':history},'findings':findings}
def _atomic(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=None
    try:
      with tempfile.NamedTemporaryFile(dir=str(path.parent),mode='w',encoding='utf-8',delete=False) as f: tmp=f.name; json.dump(payload,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
      Path(tmp).replace(path)
    finally:
      if tmp and Path(tmp).exists(): Path(tmp).unlink()
def write_quality_report(device_code=DEVICE_CODE,now=None):
    p=build_quality_report(device_code,now); _atomic(REPORT_PATH,p); HISTORY_PATH.parent.mkdir(parents=True,exist_ok=True)
    with HISTORY_PATH.open('a',encoding='utf-8') as f: fcntl.flock(f,fcntl.LOCK_EX); f.write(json.dumps(p,ensure_ascii=False,separators=(',',':'))+'\n'); f.flush(); os.fsync(f.fileno()); fcntl.flock(f,fcntl.LOCK_UN)
    return REPORT_PATH
def main():
 p=argparse.ArgumentParser(); p.add_argument('device_code',nargs='?',default=DEVICE_CODE); p.add_argument('--json',action='store_true'); a=p.parse_args(); path=write_quality_report(a.device_code); print(json.dumps(_load_json(path),ensure_ascii=False,separators=(',',':')) if a.json else path.read_text(encoding='utf-8'))
if __name__=='__main__': main()
