#!/usr/bin/env python3
"""
Arbitrum Live Torque Scanner
Pure live numbers only. No frequency assumptions.
Public RPC. Observation only. Nothing is broadcast.
"""
import json, urllib.request, math, time, sys

RPC = "https://arb1.arbitrum.io/rpc"
FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"

TOKENS = {
    "WETH": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    "ARB":  "0x912CE59144191C1204E64559FE8253a0e49E6548",
    "WBTC": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
    "LINK": "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4",
    "DAI":  "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",
    "GMX":  "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a",
    "RDNT": "0x3082CC23568eA640225c2467653dB90e9250AaA0",
    "PENDLE":"0x0c880f6761F1af8d9Aa9C466984b80DAb9a8c9e8",
}
FEES = [100, 500, 3000, 10000]

def rpc(method, params):
    payload = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req = urllib.request.Request(RPC, data=payload, headers={
        "Content-Type":"application/json","User-Agent":"Mozilla/5.0","Accept":"application/json"
    })
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.loads(r.read().decode())
        if "error" in d: raise Exception(d["error"])
        return d["result"]

def eth_call(to, data):
    return rpc("eth_call", [{"to":to,"data":data},"latest"])

def pad_addr(a): return a.lower().replace("0x","").zfill(64)
def pad_uint(n): return hex(n)[2:].zfill(64)

def get_pool(a,b,fee):
    data="0x1698ee82"+pad_addr(a)+pad_addr(b)+pad_uint(fee)
    try:
        res=eth_call(FACTORY,data)
        if not res or int(res,16)==0: return None
        return "0x"+res[-40:]
    except: return None

def slot0_price(pool):
    res=eth_call(pool,"0x3850c7bd")
    raw=res[2:]
    sqrt=int(raw[0:64],16)
    return (sqrt/(2**96))**2

def liquidity(pool):
    return int(eth_call(pool,"0x1a686502"),16)

def main():
    block=int(rpc("eth_blockNumber",[]),16)
    gp=int(rpc("eth_gasPrice",[]),16)
    p=get_pool(TOKENS["WETH"],TOKENS["USDC"],500)
    eth=slot0_price(p)*1e12
    gas_usd=450000*gp/1e18*eth

    print("="*78)
    print(" ARBITRUM LIVE TORQUE SCANNER")
    print("="*78)
    print(f"block {block} | gas {gp/1e9:.5f} gwei | ETH ${eth:.2f}")
    print(f"gas/cycle ${gas_usd:.4f} | Balancer fee $0")
    print("size capped by live thin-tier liquidity")
    print("NET = size * (gap * keep%) - gas\n")

    pairs=[]
    for name,addr in TOKENS.items():
        if name in ("WETH","USDC"): continue
        pairs.append((f"WETH-{name}", TOKENS["WETH"], addr))
    pairs += [("WETH-USDC",TOKENS["WETH"],TOKENS["USDC"]),
              ("USDC-USDT",TOKENS["USDC"],TOKENS["USDT"]),
              ("USDC-DAI",TOKENS["USDC"],TOKENS["DAI"])]

    rows=[]
    for pname,a,b in pairs:
        states={}
        for fee in FEES:
            pool=get_pool(a,b,fee)
            if not pool: continue
            try:
                price=slot0_price(pool)
                liq=liquidity(pool)
                if price>0: states[fee]={"price":price,"liq":liq}
            except: pass
            time.sleep(0.02)
        if len(states)<2: continue
        fl=sorted(states.keys())
        for i in range(len(fl)):
            for j in range(i+1,len(fl)):
                fa,fb=fl[i],fl[j]
                pa,pb=states[fa]["price"],states[fb]["price"]
                gap=abs(pa-pb)/min(pa,pb)*100
                if gap<0.20 or gap>10.0: continue
                thin=min(states[fa]["liq"],states[fb]["liq"])
                size=max(min(thin/1e16*600, 25000), 0)
                if size<200: continue
                n10=size*(gap*0.10/100)-gas_usd
                n15=size*(gap*0.15/100)-gas_usd
                n25=size*(gap*0.25/100)-gas_usd
                rows.append({"pair":pname,"fees":f"{fa}/{fb}","gap":gap,"size":size,
                             "n10":n10,"n15":n15,"n25":n25})

    rows.sort(key=lambda x: x["n15"], reverse=True)
    print(f"{'PAIR':<12} {'FEES':<10} {'GAP':>6} {'SIZE':>7} {'NET@10%':>9} {'NET@15%':>9} {'NET@25%':>9}")
    print("-"*78)
    for r in rows[:20]:
        print(f"{r['pair']:<12} {r['fees']:<10} {r['gap']:5.2f}% ${r['size']:>5.0f} ${r['n10']:>8.2f} ${r['n15']:>8.2f} ${r['n25']:>8.2f}")
    print("\nObservation only. Nothing broadcast.")

if __name__ == "__main__":
    main()
