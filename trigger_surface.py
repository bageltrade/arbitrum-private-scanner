#!/usr/bin/env python3
"""
AXION PRIVATE TRIGGER SURFACE
Something we can trigger ourselves.

Custom logic that is NOT passive arb watching.
We look for conditions we can force or amplify:

1. Thin-tier imbalance that can be pushed
2. Tick-boundary proximity (price sitting near a tick that unlocks liquidity)
3. One-sided depth (almost all liquidity on one side of current tick)
4. Self-trigger score: how much $ is required to move price by X%
5. "Ignition" surfaces: small capital can create a larger temporary gap
6. Multi-pool pressure points (same pair, different fees, one is fragile)

Read-only observation + calculation. Public RPC. No broadcast.
"""

import json
import time
import math
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Tuple

RPC = "https://arb1.arbitrum.io/rpc"

FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
USDT = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"
ARB  = "0x912CE59144191C1204E64559FE8253a0e49E6548"
WBTC = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"
LINK = "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4"
DAI  = "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"

SEL_GET_POOL  = "1698ee82"
SEL_SLOT0     = "3850c7bd"
SEL_LIQUIDITY = "1a686502"
SEL_TOKEN0    = "0dfe1681"
SEL_TOKEN1    = "d21220a7"


def rpc(method, params):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        RPC, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read().decode())
        if "error" in data:
            raise Exception(data["error"])
        return data["result"]


def eth_call(to, data):
    return rpc("eth_call", [{"to": to, "data": data}, "latest"])


def pad_addr(a):
    return a.lower().replace("0x", "").zfill(64)


def pad_uint(n):
    return hex(n)[2:].zfill(64)


def get_pool(a, b, fee):
    data = "0x" + SEL_GET_POOL + pad_addr(a) + pad_addr(b) + pad_uint(fee)
    try:
        res = eth_call(FACTORY, data)
        if not res or int(res, 16) == 0:
            return None
        return "0x" + res[-40:]
    except Exception:
        return None


def slot0(pool):
    try:
        res = eth_call(pool, "0x" + SEL_SLOT0)
        raw = res[2:]
        sqrt = int(raw[0:64], 16)
        tick = int(raw[64:128], 16)
        if tick >= 2**23:
            tick -= 2**24
        unlocked = int(raw[384:448], 16) != 0
        price = (sqrt / (2**96))**2
        return {"sqrt": sqrt, "tick": tick, "price": price, "unlocked": unlocked}
    except Exception:
        return None


def liquidity(pool):
    try:
        res = eth_call(pool, "0x" + SEL_LIQUIDITY)
        return int(res, 16) if res else 0
    except Exception:
        return 0


def token0(pool):
    try:
        res = eth_call(pool, "0x" + SEL_TOKEN0)
        return "0x" + res[-40:] if res else None
    except Exception:
        return None


@dataclass
class TriggerSurface:
    pair: str
    fee: int
    pool: str
    tick: int
    liquidity: int
    price: float
    # custom metrics
    move_cost_usd_for_50bps: float      # rough $ to move price 0.5%
    ignition_score: float               # how easy to create temporary displacement
    fragility: float                    # inverse of liquidity near tick
    self_triggerable: bool
    reason: str
    raw: Dict = field(default_factory=dict)


class TriggerScanner:
    def __init__(self):
        self.block = int(rpc("eth_blockNumber", []), 16)
        self.gas_price = int(rpc("eth_gasPrice", []), 16)
        self.surfaces: List[TriggerSurface] = []
        # live ETH price approx from earlier method
        self.eth_price = self._eth_price()
        print(f"[+] trigger scanner live | block {self.block}")
        print(f"[+] gas {self.gas_price/1e9:.5f} gwei | ETH ≈ ${self.eth_price:.2f}")

    def _eth_price(self):
        pool = get_pool(WETH, USDC, 500)
        if not pool:
            return 2500.0
        s = slot0(pool)
        if not s:
            return 2500.0
        t0 = token0(pool)
        price = s["price"]
        if t0 and t0.lower() == WETH.lower():
            return price * 1e12
        return (1 / price) * 1e12 if price else 2500.0

    def _move_cost(self, liq: int, gap_target: float = 0.005) -> float:
        """
        Extremely simplified impact proxy.
        Real concentrated liquidity is tick-based; this is a research heuristic only.
        Higher liquidity → higher cost to move.
        We invert so thin pools show low move cost.
        """
        if liq <= 0:
            return 999999.0
        # research formula: cost scales with liquidity and desired move
        # constant chosen so numbers stay human-readable
        cost = (liq / 1e18) * (gap_target * 100) * 40
        return max(cost, 0.01)

    def _ignition(self, liq: int, tick: int) -> float:
        """
        Custom ignition score:
        - lower liquidity = easier to push
        - tick near round boundaries (multiples of 10/60/200) gets small bonus
          because liquidity often concentrates at those levels
        """
        if liq <= 0:
            return 0.0
        base = 1e18 / (liq + 1)
        # tick boundary proximity bonus
        boundary_bonus = 1.0
        for step in (10, 60, 200):
            dist = abs(tick % step)
            dist = min(dist, step - dist)
            if dist <= 2:
                boundary_bonus = 1.35
                break
        return round(base * boundary_bonus * 1e6, 4)

    def scan_pool(self, a, b, fee, label):
        pool = get_pool(a, b, fee)
        if not pool:
            return
        s = slot0(pool)
        if not s:
            return
        liq = liquidity(pool)
        move_cost = self._move_cost(liq, 0.005)
        ignition = self._ignition(liq, s["tick"])
        fragility = round(1e18 / (liq + 1), 8)

        # self-triggerable if move cost is low enough that a flash-loan size can reach it
        self_trig = move_cost < 8000  # under ~$8k to move 50 bps (heuristic)

        reason_parts = []
        if self_trig:
            reason_parts.append("low move-cost → self-triggerable with modest size")
        if ignition > 50:
            reason_parts.append("high ignition (thin + near boundary)")
        if liq < 1e15:
            reason_parts.append("extremely thin active liquidity")
        if not reason_parts:
            reason_parts.append("observed")

        self.surfaces.append(TriggerSurface(
            pair=label,
            fee=fee,
            pool=pool,
            tick=s["tick"],
            liquidity=liq,
            price=s["price"],
            move_cost_usd_for_50bps=round(move_cost, 2),
            ignition_score=ignition,
            fragility=fragility,
            self_triggerable=self_trig,
            reason=" | ".join(reason_parts),
            raw={"sqrt": s["sqrt"], "unlocked": s["unlocked"]}
        ))
        time.sleep(0.06)

    def run(self):
        print("[*] scanning self-trigger surfaces…")
        pairs = [
            (WETH, USDC, [100, 500, 3000, 10000], "WETH-USDC"),
            (WETH, USDT, [100, 500, 3000, 10000], "WETH-USDT"),
            (WETH, ARB,  [500, 3000, 10000], "WETH-ARB"),
            (WETH, WBTC, [500, 3000, 10000], "WETH-WBTC"),
            (WETH, LINK, [500, 3000, 10000], "WETH-LINK"),
            (USDC, USDT, [100, 500, 3000], "USDC-USDT"),
            (USDC, ARB,  [500, 3000, 10000], "USDC-ARB"),
            (USDC, WBTC, [500, 3000, 10000], "USDC-WBTC"),
            (USDC, DAI,  [100, 500, 3000], "USDC-DAI"),
        ]
        for a, b, fees, label in pairs:
            for fee in fees:
                self.scan_pool(a, b, fee, label)
            time.sleep(0.08)

        # rank by ignition then by low move cost
        self.surfaces.sort(key=lambda x: (x.ignition_score, -x.move_cost_usd_for_50bps), reverse=True)
        return self.surfaces

    def report(self):
        print("\n" + "=" * 68)
        print("  SELF-TRIGGER SURFACE REPORT — AXION ONLY")
        print("  move-cost | ignition | fragility | self-triggerable")
        print("=" * 68)
        print(f"block {self.block} | gas {self.gas_price/1e9:.5f} gwei | ETH ${self.eth_price:.2f}\n")

        # show top triggerable first
        triggerable = [s for s in self.surfaces if s.self_triggerable]
        print(f"[+] self-triggerable surfaces: {len(triggerable)} / {len(self.surfaces)}\n")

        for i, s in enumerate(self.surfaces[:18], 1):
            flag = "★ TRIGGER" if s.self_triggerable else "  "
            print(f"{i}. {flag} {s.pair} fee={s.fee}")
            print(f"   pool {s.pool}")
            print(f"   tick={s.tick} | liq={s.liquidity}")
            print(f"   move_cost_50bps ≈ ${s.move_cost_usd_for_50bps:,.2f}")
            print(f"   ignition={s.ignition_score} | fragility={s.fragility}")
            print(f"   {s.reason}")
            print()

        path = "/home/workdir/artifacts/trigger_surfaces.json"
        with open(path, "w") as f:
            json.dump([asdict(s) for s in self.surfaces], f, indent=2)
        print(f"[+] dump → {path}")
        print("[+] pure observation. nothing sent on-chain.")


if __name__ == "__main__":
    sc = TriggerScanner()
    sc.run()
    sc.report()
