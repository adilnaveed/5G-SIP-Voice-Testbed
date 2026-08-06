#!/usr/bin/env python3
"""
qos_pipeline.py — Runs iperf3 (JSON output), extracts per-second throughput
plus one final jitter/packet-loss summary, computes MOS, and pushes all of
it into InfluxDB as a proper time-series.

Usage:
    python3 qos_pipeline.py
    python3 qos_pipeline.py --ue oai-nr-ue2 --ue-ip 10.0.0.3 --duration 15
"""

import argparse
import json
import subprocess
import time
import urllib.request

INFLUX_URL = "http://localhost:8086/api/v2/write"
INFLUX_ORG = "testbed"
INFLUX_BUCKET = "qos_metrics"
INFLUX_TOKEN = "your-influxdb-token-here"
EXT_DN_IP = "192.168.70.135"


def run_iperf3(ue_container, ue_ip, duration, bandwidth):
    cmd = [
        "docker", "exec", ue_container,
        "iperf3", "-c", EXT_DN_IP,
        "-u", "-B", ue_ip,
        "-t", str(duration), "-b", bandwidth, "-J",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 15)
    if result.returncode != 0:
        raise RuntimeError(f"iperf3 failed: {result.stderr or result.stdout}")
    return json.loads(result.stdout)


def delay_impairment(d):
    id_base = 0.024 * d
    if d > 177.3:
        id_base += 0.11 * (d - 177.3)
    return id_base


def effective_equipment_impairment(ppl, ie=0.0, bpl=4.3):
    return ie + (95.0 - ie) * (ppl / (ppl / bpl + 1.0)) if ppl > 0 else ie


def r_to_mos(r):
    if r < 0:
        return 1.0
    if r > 100:
        return 4.5
    mos = 1 + 0.035 * r + 7e-6 * r * (r - 60) * (100 - r)
    return round(min(max(mos, 1.0), 4.5), 3)


def compute_mos(jitter_ms, rtt_ms, loss_pct):
    owd = rtt_ms / 2.0 + 2.0 * jitter_ms + 15.0
    r = 93.2 - delay_impairment(owd) - effective_equipment_impairment(loss_pct)
    return r_to_mos(r), round(r, 2)


def write_line_protocol(lines):
    """Sends one or more InfluxDB line-protocol lines in a single write request."""
    data = "\n".join(lines).encode("utf-8")
    url = f"{INFLUX_URL}?org={INFLUX_ORG}&bucket={INFLUX_BUCKET}&precision=s"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Token {INFLUX_TOKEN}")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    with urllib.request.urlopen(req) as resp:
        return resp.status


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ue", default="oai-nr-ue")
    parser.add_argument("--ue-ip", default="10.0.0.2")
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--bandwidth", default="5M")
    parser.add_argument("--rtt", type=float, default=74.5,
                         help="RTT estimate in ms, used for MOS calculation")
    args = parser.parse_args()

    print(f"Running iperf3: {args.ue} ({args.ue_ip}) -> {EXT_DN_IP}, "
          f"{args.duration}s @ {args.bandwidth}...")
    result = run_iperf3(args.ue, args.ue_ip, args.duration, args.bandwidth)

    # Real start time of the test, from iperf3's own timestamp
    test_start_unix = result["start"]["timestamp"]["timesecs"]

    # --- Per-second throughput points ---
    lines = []
    print("\nPer-second throughput:")
    for interval in result["intervals"]:
        s = interval["sum"]
        point_time = int(test_start_unix + s["start"])
        mbps = round(s["bits_per_second"] / 1_000_000, 3)
        print(f"  t={s['start']:.0f}-{s['end']:.0f}s : {mbps} Mbps")
        lines.append(f"call_quality,ue={args.ue},metric=throughput throughput_mbps={mbps} {point_time}")

    # --- Final jitter / packet-loss summary (one point) ---
    end_summary = result["end"]["sum"]
    jitter_ms = round(end_summary["jitter_ms"], 3)
    loss_pct = round(end_summary["lost_percent"], 3)
    avg_mbps = round(end_summary["bits_per_second"] / 1_000_000, 3)

    mos, r_factor = compute_mos(jitter_ms, args.rtt, loss_pct)

    print(f"\nFinal summary:")
    print(f"  Jitter        : {jitter_ms} ms")
    print(f"  Packet loss   : {loss_pct}%")
    print(f"  Avg throughput: {avg_mbps} Mbps")
    print(f"  Predicted MOS : {mos} (R-factor {r_factor})")

    final_time = int(test_start_unix + args.duration)
    lines.append(
        f"call_quality,ue={args.ue},metric=summary "
        f"jitter_ms={jitter_ms},packet_loss_pct={loss_pct},"
        f"avg_throughput_mbps={avg_mbps},rtt_ms={args.rtt},"
        f"mos={mos},r_factor={r_factor} {final_time}"
    )

    status = write_line_protocol(lines)
    print(f"\nPushed {len(lines)} data points to InfluxDB: status {status}")
