#!/usr/bin/env python3
"""
capture_call_quality.py — Extracts REAL SIP call-quality metrics (jitter, RTT,
packet loss) from RTPengine's own per-call summary log line, computes a
predicted MOS, and pushes it to InfluxDB — tagged separately from iperf data
so both KPI sources can be compared on the same Grafana dashboard.

RTPengine automatically logs a line like this at the end of every call:
    respective (avg/min/max) jitter 12/12/12 ms, RTT-e2e 74.5/85.8/85.8 ms,
    RTT-dsct 30.5/41.8/41.8 ms, packet loss 0/0/0%

This is genuine, per-call voice-quality telemetry — more directly relevant
to "how good was this actual call" than a raw iperf throughput test.

Usage:
    # After placing and ending a SIP call:
    python3 capture_call_quality.py

    # Or watch continuously and auto-push every new call as it completes:
    python3 capture_call_quality.py --watch
"""

import argparse
import re
import subprocess
import time
import urllib.request

INFLUX_URL = "http://localhost:8086/api/v2/write"
INFLUX_ORG = "testbed"
INFLUX_BUCKET = "qos_metrics"
INFLUX_TOKEN = "your-influxdb-token-here"

# Matches RTPengine's per-call summary line, e.g.:
# "respective (avg/min/max) jitter 12/12/12 ms, RTT-e2e 74.5/85.8/85.8 ms,
#  RTT-dsct 30.5/41.8/41.8 ms, packet loss 0/0/0%"
SUMMARY_RE = re.compile(
    r"jitter\s+([\d.]+)/([\d.]+)/([\d.]+)\s+ms,\s+"
    r"RTT-e2e\s+([\d.]+)/([\d.]+)/([\d.]+)\s+ms,\s+"
    r"RTT-dsct\s+([\d.]+)/([\d.]+)/([\d.]+)\s+ms,\s+"
    r"packet loss\s+([\d.]+)/([\d.]+)/([\d.]+)%"
)


def get_rtpengine_logs(tail=200):
    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), "rtpengine"],
        capture_output=True, text=True,
    )
    return result.stdout + result.stderr


def find_latest_call_summary(log_text):
    """Returns the most recent parsed summary dict, or None if none found."""
    matches = list(SUMMARY_RE.finditer(log_text))
    if not matches:
        return None
    m = matches[-1]
    return {
        "jitter_avg": float(m.group(1)),
        "jitter_min": float(m.group(2)),
        "jitter_max": float(m.group(3)),
        "rtt_e2e_avg": float(m.group(4)),
        "rtt_e2e_min": float(m.group(5)),
        "rtt_e2e_max": float(m.group(6)),
        "rtt_dsct_avg": float(m.group(7)),
        "loss_avg": float(m.group(10)),
    }


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


def push_to_influx(summary, mos, r_factor):
    fields = (
        f"jitter_ms={summary['jitter_avg']},"
        f"rtt_ms={summary['rtt_e2e_avg']},"
        f"packet_loss_pct={summary['loss_avg']},"
        f"mos={mos},"
        f"r_factor={r_factor}"
    )
    line = f"call_quality,ue=sip_call,metric=real_call {fields}"
    data = line.encode("utf-8")
    url = f"{INFLUX_URL}?org={INFLUX_ORG}&bucket={INFLUX_BUCKET}&precision=s"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Token {INFLUX_TOKEN}")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    with urllib.request.urlopen(req) as resp:
        return resp.status


def run_once():
    logs = get_rtpengine_logs()
    summary = find_latest_call_summary(logs)
    if not summary:
        print("No call summary found in recent RTPengine logs. "
              "Place and end a SIP call first, then re-run.")
        return

    mos, r = compute_mos(summary["jitter_avg"], summary["rtt_e2e_avg"], summary["loss_avg"])

    print("Real SIP call quality (from RTPengine):")
    print(f"  Jitter (avg/min/max): {summary['jitter_avg']}/{summary['jitter_min']}/{summary['jitter_max']} ms")
    print(f"  RTT e2e (avg/min/max): {summary['rtt_e2e_avg']}/{summary['rtt_e2e_min']}/{summary['rtt_e2e_max']} ms")
    print(f"  Packet loss (avg): {summary['loss_avg']}%")
    print(f"  Predicted MOS: {mos} (R-factor {r})")

    status = push_to_influx(summary, mos, r)
    print(f"Pushed to InfluxDB: status {status}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true",
                         help="Poll continuously, pushing each new call summary as it appears")
    parser.add_argument("--interval", type=int, default=15,
                         help="Seconds between polls in --watch mode")
    args = parser.parse_args()

    if not args.watch:
        run_once()
    else:
        seen = set()
        print(f"Watching for new call summaries every {args.interval}s... (Ctrl+C to stop)")
        while True:
            logs = get_rtpengine_logs(tail=500)
            for m in SUMMARY_RE.finditer(logs):
                key = m.group(0)
                if key not in seen:
                    seen.add(key)
                    summary = find_latest_call_summary(key)
                    mos, r = compute_mos(summary["jitter_avg"], summary["rtt_e2e_avg"], summary["loss_avg"])
                    print(f"New call found -> MOS {mos} (jitter {summary['jitter_avg']}ms, "
                          f"RTT {summary['rtt_e2e_avg']}ms, loss {summary['loss_avg']}%)")
                    push_to_influx(summary, mos, r)
            time.sleep(args.interval)
