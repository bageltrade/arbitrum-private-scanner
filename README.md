# Arbitrum Live Torque Scanner

Private research scanner for Arbitrum One.

## What it does

- Connects to public Arbitrum RPC (`https://arb1.arbitrum.io/rpc`)
- Reads live Uniswap V3 pool state across fee tiers (100 / 500 / 3000 / 10000)
- Measures **live price gaps** between tiers for many token pairs
- Caps trade size by **live thin-tier liquidity**
- Subtracts **live gas cost**
- Prints net edge at 10% / 15% / 25% keep-rates of the measured gap

No frequency assumptions. No hidden multipliers. Pure measured numbers.

## Requirements

- Python 3.9+
- No extra packages (uses only stdlib)

## Run

```bash
python3 live_torque_scanner.py
```

## Other scripts in this repo

| File | Purpose |
|------|---------|
| `live_torque_scanner.py` | Main pure-live scanner (recommended) |
| `scanner_v2_raw.py` | Earlier multi-layer private surface scanner |
| `trigger_surface.py` | Self-trigger / move-cost oriented scan |
| `special_scanner.py` | Original special scoring version |

## Important

- **Observation only.** Nothing is signed or broadcast.
- Public RPC only. No private keys required.
- Research / educational use.

## License

Private. For the owner of this repository only.
