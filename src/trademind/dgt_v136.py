"""TradeMind v1.36 clean-room DGT paper reproduction.

Implements the public 2025 paper "Dynamic Grid Trading Strategy: From Zero
Expectation to Market Outperformance" (arXiv:2506.11921) as a read-only
historical test. The paper window (2021-01 through 2024-07) is used to select
parameters. Parameters are then frozen and evaluated on a post-paper holdout.

The implementation uses the geometric grid described in the paper, the public
parameter ranges from the authors' reference repository, 8 bps fee assumption,
and Binance Public Data monthly 1-minute spot archives. Every external capital
top-up is recorded. It reports both the paper-style annualized return proxy and
a cash-flow-aware XIRR so capital injections cannot be hidden.

No orders, no account API, no publication.
"""
from __future__ import annotations
import argparse, bisect, csv, io, json, math, urllib.request, zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

VERSION="1.36.0"
VISION="https://data.binance.vision/data/spot/monthly/klines"
GRID_SIZES=(0.005,0.01,0.015,0.02,0.03)
HALF_GRIDS=(2,3,5)
FEE=0.0008
PRINCIPAL=100.0
YEAR_MS=365.25*24*3600*1000

@dataclass(frozen=True, slots=True)
class Bar:
    t:int; o:float; h:float; l:float; c:float

@dataclass(frozen=True, slots=True)
class Result:
    symbol:str; start:int; end:int; grid:float; half:int; bars:int; resets:int; crosses:int
    up_resets:int; down_resets:int; topups:int; input_money:float; final_value:float
    total_return:float; ann_proxy:float; xirr:float|None; bh_return:float; bh_cagr:float
    def obj(self):
        return {
            "symbol":self.symbol,"start":iso(self.start),"end":iso(self.end),"grid_size":self.grid,
            "half_grids":self.half,"bars":self.bars,"resets":self.resets,"crosses":self.crosses,
            "up_resets":self.up_resets,"down_resets":self.down_resets,"capital_topups":self.topups,
            "input_money":self.input_money,"final_value":self.final_value,
            "total_return":self.total_return,"annualized_return_proxy":self.ann_proxy,
            "xirr":self.xirr,"buy_hold_return":self.bh_return,"buy_hold_cagr":self.bh_cagr,
        }

def iso(ms:int)->str:
    return datetime.fromtimestamp(ms/1000,tz=timezone.utc).isoformat()

def parse_iso(s:str)->int:
    d=datetime.fromisoformat(s.replace("Z","+00:00"))
    if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
    return int(d.astimezone(timezone.utc).timestamp()*1000)

def months(start:int,end:int):
    d=datetime.fromtimestamp(start/1000,tz=timezone.utc)
    cur=datetime(d.year,d.month,1,tzinfo=timezone.utc)
    e=datetime.fromtimestamp((end-1)/1000,tz=timezone.utc)
    last=datetime(e.year,e.month,1,tzinfo=timezone.utc)
    while cur<=last:
        yield cur.year,cur.month
        cur=datetime(cur.year+(cur.month==12),1 if cur.month==12 else cur.month+1,1,tzinfo=timezone.utc)

def norm_ts(v:str)->int:
    x=int(float(v))
    return x//1000 if x>=10**15 else x

def get_zip(symbol:str,y:int,m:int,root:Path,refresh:bool)->Path:
    folder=root/symbol/"1m"; folder.mkdir(parents=True,exist_ok=True)
    name=f"{symbol}-1m-{y:04d}-{m:02d}.zip"; p=folder/name
    if p.exists() and p.stat().st_size>100 and not refresh: return p
    url=f"{VISION}/{symbol}/1m/{name}"
    print("download",symbol,f"{y:04d}-{m:02d}",flush=True)
    req=urllib.request.Request(url,headers={"User-Agent":"TradeMindAI/1.36"})
    with urllib.request.urlopen(req,timeout=90) as r: data=r.read()
    tmp=p.with_suffix(".zip.tmp"); tmp.write_bytes(data)
    with zipfile.ZipFile(tmp) as z:
        bad=z.testzip()
        if bad: raise ValueError(f"corrupt archive member {bad}")
    tmp.replace(p); return p

def read_zip(p:Path,start:int,end:int)->list[Bar]:
    out=[]
    with zipfile.ZipFile(p) as z:
        names=[n for n in z.namelist() if not n.endswith("/")]
        if not names:return out
        with z.open(names[0]) as raw:
            rd=csv.reader(io.TextIOWrapper(raw,encoding="utf-8-sig",newline=""))
            for f in rd:
                if len(f)<5: continue
                try:
                    t=norm_ts(f[0]); o,h,l,c=map(float,f[1:5])
                except ValueError: continue
                if start<=t<end and min(o,h,l,c)>0 and h>=l: out.append(Bar(t,o,h,l,c))
    return out

def history(symbol:str,start:int,end:int,root:Path,refresh:bool)->list[Bar]:
    rows=[]
    for y,m in months(start,end):
        rows+=read_zip(get_zip(symbol,y,m,root,refresh),start,end)
    uniq={b.t:b for b in rows}
    bars=[uniq[k] for k in sorted(uniq)]
    if len(bars)<1000: raise ValueError(f"{symbol}: only {len(bars)} bars")
    return bars

def levels(center:float,k:float,half:int)->list[float]:
    r=1+k
    return [center*(r**i) for i in range(-half,half+1)]

def p_up(n:int,count:int,k:float,M:float,fee:float)->float:
    return count*(count+1)/2*(M/n)*(k-2*fee)

def p_arb(n:int,ref:int,k:float,trades:int,M:float,fee:float)->float:
    return ((trades-ref)/2)*(M/n)*(k-2*fee)

def npv(rate:float,flows:list[tuple[int,float]])->float:
    t0=flows[0][0]
    return sum(v/((1+rate)**((t-t0)/YEAR_MS)) for t,v in flows)

def calc_xirr(flows:list[tuple[int,float]])->float|None:
    if not any(v<0 for _,v in flows) or not any(v>0 for _,v in flows): return None
    lo,hi=-0.9999,1.0; flo,fhi=npv(lo,flows),npv(hi,flows)
    for _ in range(40):
        if flo*fhi<=0: break
        hi=hi*2+1; fhi=npv(hi,flows)
    if flo*fhi>0:return None
    for _ in range(100):
        mid=(lo+hi)/2; fm=npv(mid,flows)
        if flo*fm<=0: hi=mid
        else: lo=mid; flo=fm
    return (lo+hi)/2

def simulate(symbol:str,bars:Sequence[Bar],k:float,half:int,M:float=PRINCIPAL,fee:float=FEE)->Result:
    n=2*half
    center=bars[0].o; gl=levels(center,k,half); lower,upper,current=gl[0],gl[-1],half
    usdt=coin=0.0; input_money=M; trades=crosses=0; resets=up=down=0; topups=1
    flows=[(bars[0].t,-M)]
    def fund(when:int):
        nonlocal usdt,input_money,topups
        if usdt>=M: usdt-=M
        else:
            need=M-usdt
            if need>1e-12:
                input_money+=need; topups+=1; flows.append((when,-need))
            usdt=0.0
    prev=bars[0].o
    for ix,b in enumerate(bars):
        path=[b.o,b.l,b.h,b.c]
        if ix:path[0]=prev
        for a,z in zip(path,path[1:]):
            if a<z:
                while current<n and a<=gl[current+1]<z:
                    current+=1; trades+=1; crosses+=1
            elif a>z:
                while current>0 and z<=gl[current-1]<a:
                    current-=1; trades+=1; crosses+=1
            if z>upper or current==n:
                usdt+=p_up(n,half,k,M,fee)+p_arb(n,half,k,trades,M,fee)+M
                resets+=1;up+=1;trades=0;fund(b.t)
                center=z;gl=levels(center,k,half);lower,upper,current=gl[0],gl[-1],half
            elif z<lower or current==0:
                usdt+=p_arb(n,half,k,trades,M,fee)
                coin+=(M/2)/center*(1-2*fee)
                for lvl in gl[:half]: coin+=(M/n)/lvl*(1-2*fee)
                resets+=1;down+=1;trades=0;fund(b.t)
                center=z;gl=levels(center,k,half);lower,upper,current=gl[0],gl[-1],half
        prev=b.c
    close=bars[-1].c; mid=half; per=M/n; mid_coin=(M/2)/gl[mid]*(1-fee)
    idx=max(0,min(n,bisect.bisect_right(gl,close)-1))
    if close>=gl[mid]:
        remain=n-idx; usdt+=remain*per
        cnt=max(0,idx-mid)
        usdt+=cnt*per+p_up(n,cnt,k,M,fee)+p_arb(n,cnt,k,trades,M,fee)
        coin+=mid_coin*(remain/half)
    else:
        cnt=max(0,mid-idx)
        for j in range(mid-1,idx,-1): coin+=(per/gl[j])*(1-2*fee)
        usdt+=p_arb(n,cnt,k,trades,M,fee)+(half-cnt)*per; coin+=mid_coin
    final=usdt+coin*close
    flows.append((bars[-1].t+60000,final))
    years=max((bars[-1].t-bars[0].t)/YEAR_MS,1/365.25)
    ratio=final/input_money
    ann=ratio**(1/years)-1 if ratio>0 else -1.0
    bh=close/bars[0].o
    return Result(symbol,bars[0].t,bars[-1].t+60000,k,half,len(bars),resets,crosses,up,down,topups,input_money,final,
                  ratio-1,ann,calc_xirr(flows),bh-1,bh**(1/years)-1)

def pct(x:float|None)->str:
    return "NA" if x is None or not math.isfinite(x) else f"{100*x:.2f}%"

def write_csv(path:Path,results:list[Result]):
    rows=[r.obj() for r in results]
    with path.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

def main(argv=None)->int:
    ap=argparse.ArgumentParser(description="TradeMind v1.36 DGT paper reproduction")
    ap.add_argument("--symbols",default="BTCUSDT,ETHUSDT")
    ap.add_argument("--paper-start",default="2021-01-01T00:00:00+00:00")
    ap.add_argument("--paper-end",default="2024-08-01T00:00:00+00:00")
    ap.add_argument("--holdout-end",default="2026-08-01T00:00:00+00:00")
    ap.add_argument("--cache-dir",type=Path,default=Path("data/dgt_v136/binance_1m"))
    ap.add_argument("--output-dir",type=Path,default=Path("data/dgt_v136/results"))
    ap.add_argument("--refresh",action="store_true")
    a=ap.parse_args(argv)
    s0,s1,s2=map(parse_iso,(a.paper_start,a.paper_end,a.holdout_end))
    if not s0<s1<s2: raise ValueError("bad date windows")
    a.output_dir.mkdir(parents=True,exist_ok=True)
    summary={"schema_version":VERSION,"state":"OK","paper":"arXiv:2506.11921","read_only":True,"symbols":{}}
    for symbol in [x.strip().upper() for x in a.symbols.split(",") if x.strip()]:
        print(f"\n===== {symbol} =====",flush=True)
        bars=history(symbol,s0,s2,a.cache_dir,a.refresh)
        train=[b for b in bars if b.t<s1];test=[b for b in bars if b.t>=s1]
        print(f"paper bars={len(train):,} holdout bars={len(test):,}")
        results=[]
        for k in GRID_SIZES:
            for h in HALF_GRIDS:
                r=simulate(symbol,train,k,h);results.append(r)
                print(f"PAPER g={k:.3%} h={h} ann={pct(r.ann_proxy)} xirr={pct(r.xirr)} topups={r.topups}",flush=True)
        results.sort(key=lambda r:(r.ann_proxy,r.xirr if r.xirr is not None else -999),reverse=True)
        best=results[0];write_csv(a.output_dir/f"{symbol}_paper_grid.csv",results)
        hold=simulate(symbol,test,best.grid,best.half)
        print(f"BEST PAPER {symbol}: g={best.grid:.3%} h={best.half} ann={pct(best.ann_proxy)} xirr={pct(best.xirr)}")
        print(f"FROZEN HOLDOUT {symbol}: ann={pct(hold.ann_proxy)} xirr={pct(hold.xirr)} return={pct(hold.total_return)} B&H_CAGR={pct(hold.bh_cagr)} topups={hold.topups}")
        summary["symbols"][symbol]={"best_paper":best.obj(),"holdout":hold.obj()}
    p=a.output_dir/"status.json";p.write_text(json.dumps(summary,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print("\n===== V1.36 DGT SUMMARY =====")
    for s,d in summary["symbols"].items():
        b=d["best_paper"];h=d["holdout"]
        print(f"{s} | PAPER ann={100*b['annualized_return_proxy']:.2f}% xirr={pct(b['xirr'])} g={100*b['grid_size']:.2f}% h={b['half_grids']} | HOLDOUT ann={100*h['annualized_return_proxy']:.2f}% xirr={pct(h['xirr'])} return={100*h['total_return']:.2f}% topups={h['capital_topups']}")
    print("READ-ONLY. Parameters selected on paper window, then frozen for holdout.")
    print("Output:",p.resolve())
    return 0
if __name__=="__main__": raise SystemExit(main())
