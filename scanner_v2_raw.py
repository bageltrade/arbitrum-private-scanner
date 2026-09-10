#!/usr/bin/env python3
"""
ARBITRUM PRIVATE SURFACE SCANNER v2 (raw JSON-RPC)
Built only for Axion. No web3 dependency. Pure requests.

Extra layers normal people never get:
- Shadow-tick divergence
- Ghost liquidity + high-impact bias
- Direction-corrected multi-hop residual
- Capital efficiency ranking
- Private score that over-weights thin high-edge surfaces
"""

import json
import time
import math
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional

RPCS = [
    "https://arb1.arbitrum.io/rpc",
    "https://arbitrum.llamarpc.com",
]

FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
WETH  = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC  = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
USDT  = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"
ARB   = "0x912CE59144191C1204E64559FE8253a0e49E6548"
WBTC  = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"
DAI   = "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"
LINK  = "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4"

# function selectors
SEL_GET_POOL = "1698ee82"          # getPool(address,address,uint24)
SEL_SLOT0    = "3850c7bd"          # slot0()
SEL_LIQUIDITY = "1a686502"         # liquidity()
SEL_TOKEN0   = "0dfe1681"          # token0()
SEL_TOKEN1   = "d21220a7"          # token1()


def checksum(addr: str) -> str:
    return addr.lower()  # sufficient for RPC


def pad_addr(addr: str) -> str:
    return addr.lower().replace("0x", "").zfill(64)


def pad_uint(n: int) -> str:
    return hex(n)[2:].zfill(64)


def rpc(method: str, params: list, rpc_url: str = None):
    urls = [rpc_url] if rpc_url else RPCS
    last_err = None
    for url in urls:
        try:
            payload = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params
            }).encode()
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "AxionPrivateScanner/2.0"}
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode())
                if "error" in data:
                    last_err = data["error"]
                    continue
                return data.get("result")
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"rpc failed: {last_err}")


def eth_call(to: str, data: str) -> str:
    return rpc("eth_call", [{"to": to, "data": data}, "latest"])


def get_block() -> int:
    return int(rpc("eth_blockNumber", []), 16)


def get_pool(token_a: str, token_b: str, fee: int) -> Optional[str]:
    # getPool(address,address,uint24)
    data = "0x" + SEL_GET_POOL + pad_addr(token_a) + pad_addr(token_b) + pad_uint(fee)
    try:
        res = eth_call(FACTORY, data)
        if not res or res == "0x" or int(res, 16) == 0:
            return None
        return "0x" + res[-40:]
    except Exception:
        return None


def slot0(pool: str) -> Optional[Dict]:
    try:
        res = eth_call(pool, "0x" + SEL_SLOT0)
        if not res or len(res) < 2 + 64 * 7:
            return None
        # slot0 returns: sqrtPriceX96, tick, observationIndex, observationCardinality,
        # observationCardinalityNext, feeProtocol, unlocked
        raw = res[2:]
        sqrt = int(raw[0:64], 16)
        tick = int(raw[64:128], 16)
        # tick is int24, sign extend
        if tick >= 2 ** 23:
            tick -= 2 ** 24
        unlocked = int(raw[384:448], 16) != 0
        price = (sqrt / (2 ** 96)) ** 2
        tick_price = (1.0001 ** tick) if tick != 0 else 0.0
        return {
            "sqrtPriceX96": sqrt,
            "tick": tick,
            "price": price,
            "tick_price": tick_price,
            "unlocked": unlocked,
        }
    except Exception:
        return None


def liquidity(pool: str) -> int:
    try:
        res = eth_call(pool, "0x" + SEL_LIQUIDITY)
        return int(res, 16) if res else 0
    except Exception:
        return 0


def token0(pool: str) -> Optional[str]:
    try:
        res = eth_call(pool, "0x" + SEL_TOKEN0)
        return "0x" + res[-40:] if res else None
    except Exception:
        return None


@dataclass
class Surface:
    kind: str
    private_score: float
    pair: str
    gap_pct: float
    liquidity: int
    fee_tiers: List[int]
    pools: Dict[str, str]
    flash_loanable: bool
    capital_efficiency: float
    special_tags: List[str]
    reason: str
    raw: Dict = field(default_factory=dict)


class PrivateScanner:
    def __init__(self):
        self.surfaces: List[Surface] = []
        self.block = get_block()
        print(f"[+] private raw scanner live | block {self.block}")

    def _score(self, gap: float, liq: int, tags: List[str]) -> float:
        if gap <= 0:
            return 0.0
        liq_factor = math.log10(max(liq, 1) + 1)
        score = gap * liq_factor
        if liq < 10 ** 16 and gap > 0.2:
            score *= 1.7
        if liq < 10 ** 15 and gap > 0.35:
            score *= 2.1
        if "ghost" in tags:
            score *= 1.55
        if "shadow_tick" in tags:
            score *= 1.4
        if "multi_hop" in tags:
            score *= 1.6
        if "high_impact" in tags:
            score *= 1.8
        return round(score, 4)

    def _efficiency(self, gap: float, liq: int) -> float:
        if liq <= 0 or gap <= 0:
            return 0.0
        return round((gap * 100) / math.log10(liq + 10), 4)

    def scan_pair(self, t0: str, t1: str, fees: List[int], label: str):
        data = {}
        for fee in fees:
            pool = get_pool(t0, t1, fee)
            if not pool:
                continue
            s = slot0(pool)
            if not s:
                continue
            liq = liquidity(pool)
            data[fee] = {"pool": pool, "liq": liq, **s}
            time.sleep(0.05)

        if len(data) < 2:
            return

        fees_s = sorted(data.keys())
        prices = [data[f]["price"] for f in fees_s]
        pmin, pmax = min(prices), max(prices)
        if pmin <= 0:
            return
        gap = abs(pmax - pmin) / pmin * 100
        total_liq = sum(d["liq"] for d in data.values())
        min_liq = min(d["liq"] for d in data.values())
        max_liq = max(d["liq"] for d in data.values())

        tags = []
        reasons = []

        if gap > 0.07:
            tags.append("cross_fee")
            reasons.append(f"cross-fee gap {gap:.4f}%")

        if max_liq > 0 and (min_liq / max_liq) < 0.08 and gap > 0.1:
            tags.append("ghost")
            tags.append("high_impact")
            reasons.append("ghost-liquidity (thin tier exists)")

        for f, d in data.items():
            if d["price"] > 0 and d["tick_price"] > 0:
                shadow = abs(d["price"] - d["tick_price"]) / d["price"] * 100
                if shadow > 0.05:
                    tags.append("shadow_tick")
                    reasons.append(f"shadow-tick on fee {f}")
                    break

        for f, d in data.items():
            if not d["unlocked"]:
                tags.append("locked")
                reasons.append(f"unlocked=false on fee {f}")

        if min_liq < 5 * 10 ** 15 and gap > 0.25 and "high_impact" not in tags:
            tags.append("high_impact")

        if not tags:
            return

        use_liq = min_liq if "ghost" in tags else total_liq
        score = self._score(gap, use_liq, tags)
        eff = self._efficiency(gap, use_liq)

        self.surfaces.append(Surface(
            kind="+".join(tags),
            private_score=score,
            pair=label,
            gap_pct=round(gap, 5),
            liquidity=use_liq,
            fee_tiers=fees_s,
            pools={str(f): data[f]["pool"] for f in fees_s},
            flash_loanable=True,
            capital_efficiency=eff,
            special_tags=tags,
            reason=" | ".join(reasons),
            raw={
                "prices": {str(f): data[f]["price"] for f in fees_s},
                "liqs": {str(f): data[f]["liq"] for f in fees_s},
                "ticks": {str(f): data[f]["tick"] for f in fees_s},
            }
        ))

    def scan_multi_hop(self):
        def best_price(a, b, fees=(500, 3000, 10000)):
            best_p, best_l = None, -1
            for fee in fees:
                p = get_pool(a, b, fee)
                if not p:
                    continue
                s = slot0(p)
                if not s:
                    continue
                liq = liquidity(p)
                t0 = token0(p)
                if liq > best_l and s["price"] > 0:
                    best_l = liq
                    if t0 and t0.lower() == a.lower():
                        best_p = s["price"]
                    else:
                        best_p = 1.0 / s["price"]
                time.sleep(0.04)
            return best_p, best_l

        legs = [(WETH, USDC), (USDC, USDT), (USDT, WETH)]
        prices, liqs = [], []
        for a, b in legs:
            p, l = best_price(a, b)
            if p is None:
                return
            prices.append(p)
            liqs.append(l)

        product = prices[0] * prices[1] * prices[2]
        residual = abs(product - 1.0) * 100
        if residual < 0.12:
            return

        min_l = min(liqs)
        tags = ["multi_hop"]
        if min_l < 10 ** 16:
            tags.append("high_impact")
        score = self._score(residual, min_l, tags)
        eff = self._efficiency(residual, min_l)

        self.surfaces.append(Surface(
            kind="multi_hop_residual",
            private_score=score,
            pair="WETH→USDC→USDT→WETH",
            gap_pct=round(residual, 5),
            liquidity=min_l,
            fee_tiers=[],
            pools={},
            flash_loanable=True,
            capital_efficiency=eff,
            special_tags=tags,
            reason="direction-corrected 3-leg residual — private layer",
            raw={"prices": prices, "product": product, "liqs": liqs}
        ))

    def run(self):
        print("[*] running private layers…")
        pairs = [
            (WETH, USDC, [100, 500, 3000, 10000], "WETH-USDC"),
            (WETH, USDT, [100, 500, 3000, 10000], "WETH-USDT"),
            (WETH, ARB,  [500, 3000, 10000], "WETH-ARB"),
            (WETH, WBTC, [500, 3000, 10000], "WETH-WBTC"),
            (WETH, LINK, [500, 3000, 10000], "WETH-LINK"),
            (USDC, USDT, [100, 500, 3000], "USDC-USDT"),
            (USDC, ARB,  [500, 3000, 10000], "USDC-ARB"),
            (USDC, DAI,  [100, 500, 3000], "USDC-DAI"),
            (USDC, WBTC, [500, 3000, 10000], "USDC-WBTC"),
        ]
        for a, b, fees, label in pairs:
            self.scan_pair(a, b, fees, label)
            time.sleep(0.1)
        self.scan_multi_hop()
        self.surfaces.sort(key=lambda s: (s.private_score, s.capital_efficiency), reverse=True)
        return self.surfaces

    def report(self):
        print("\n" + "=" * 66)
        print("  PRIVATE SURFACE REPORT v2 — AXION ONLY")
        print("  ghost | shadow-tick | high-impact bias | multi-hop | efficiency")
        print("=" * 66)
        print(f"block: {self.block}")
        print(f"surfaces: {len(self.surfaces)}\n")

        for i, s in enumerate(self.surfaces[:15], 1):
            print(f"{i}. score={s.private_score:<8} eff={s.capital_efficiency:<7} [{s.kind}]")
            print(f"   {s.pair} | gap={s.gap_pct}% | liq={s.liquidity}")
            print(f"   tags: {s.special_tags}")
            print(f"   {s.reason}")
            if s.pools:
                print(f"   pools: {s.pools}")
            print()

        path = "/home/workdir/artifacts/private_surfaces_v2.json"
        with open(path, "w") as f:
            json.dump([asdict(s) for s in self.surfaces], f, indent=2)
        print(f"[+] dump → {path}")
        print("[+] observation only. nothing broadcast.")


if __name__ == "__main__":
    s = PrivateScanner()
    s.run()
    s.report()
