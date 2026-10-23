#!/usr/bin/env python3
"""
Single-Node Density & Cgroup Freezing Benchmark for gVisor (Substrate).
Tests 50 co-located gVisor sandboxes with Approach A clock coordination
and pre-opened FD cgroup v2 control.
"""

import os
import sys
import time
import json
import subprocess
import statistics
from typing import List, Dict, Tuple

RUNSC_BIN = "/tmp/density_test/bin/runsc"
RUNSC_ROOT = "/tmp/density_test/runsc_root"
BUNDLE_DIR = "/tmp/density_test/test_bundle"
CGROUP_BASE = "/sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/density_bench"
NUM_SANDBOXES = int(sys.argv[1]) if len(sys.argv) > 1 else 50
SAMPLE_DURATION_SEC = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0

class Sandbox:
    def __init__(self, idx: int):
        self.idx = idx
        self.id = f"sb_{idx:03d}"
        self.cgroup_dir = os.path.join(CGROUP_BASE, self.id)
        self.pid = -1
        self.freeze_fd = -1
        self.events_fd = -1

    def create_and_start(self):
        os.makedirs(self.cgroup_dir, exist_ok=True)
        # Launch runsc container
        cmd = [
            RUNSC_BIN,
            "--rootless",
            "--network=none",
            f"--root={RUNSC_ROOT}",
            "run",
            "--detach",
            f"--bundle={BUNDLE_DIR}",
            self.id
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        # Get PID
        state_cmd = [RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "state", self.id]
        state_out = subprocess.run(state_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        state_data = json.loads(state_out.stdout)
        self.pid = state_data["pid"]

        # Move to cgroup
        procs_path = os.path.join(self.cgroup_dir, "cgroup.procs")
        with open(procs_path, "w") as f:
            f.write(f"{self.pid}\n")

        # Open pre-opened FDs (mimicking cgroupstats.OpenFreezer)
        freeze_path = os.path.join(self.cgroup_dir, "cgroup.freeze")
        events_path = os.path.join(self.cgroup_dir, "cgroup.events")
        self.freeze_fd = os.open(freeze_path, os.O_RDWR)
        self.events_fd = os.open(events_path, os.O_RDONLY)

    def freeze(self) -> float:
        """Approach A freeze: Pause via URPC control socket, then cgroup.freeze=1."""
        t0 = time.perf_counter()
        
        # Phase 1: URPC Pause
        cmd = [RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "pause", self.id]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        # Phase 2: Host cgroup freeze via pre-opened FD
        os.pwrite(self.freeze_fd, b"1\n", 0)
        
        # Poll events FD until frozen 1
        while True:
            n = os.pread(self.events_fd, 256, 0)
            if b"frozen 1" in n:
                break
            time.sleep(0.0001)

        t1 = time.perf_counter()
        return (t1 - t0) * 1000.0 # ms

    def thaw(self) -> Tuple[float, float]:
        """Approach A thaw: cgroup.freeze=0, then Resume via URPC control socket."""
        t0 = time.perf_counter()
        
        # Phase 1: Host cgroup thaw via pre-opened FD
        os.pwrite(self.freeze_fd, b"0\n", 0)
        
        # Poll events FD until frozen 0
        while True:
            n = os.pread(self.events_fd, 256, 0)
            if b"frozen 0" in n:
                break
            time.sleep(0.0001)

        t_thaw = time.perf_counter()
        cgroup_thaw_ms = (t_thaw - t0) * 1000.0

        # Phase 2: URPC Resume
        cmd = [RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "resume", self.id]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        t_end = time.perf_counter()
        total_thaw_ms = (t_end - t0) * 1000.0
        return cgroup_thaw_ms, total_thaw_ms

    def read_guest_clock(self) -> Dict[str, int]:
        cmd = [RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "exec", self.id, "/bin/print_clock"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        parts = res.stdout.strip().split()
        data = {}
        for part in parts:
            k, v = part.split(":")
            data[k] = int(v)
        return data

    def cleanup(self):
        try:
            if self.freeze_fd >= 0:
                os.close(self.freeze_fd)
                self.freeze_fd = -1
            if self.events_fd >= 0:
                os.close(self.events_fd)
                self.events_fd = -1
        except Exception:
            pass

        subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "kill", self.id],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.05)
        subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "delete", self.id],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.rmdir(self.cgroup_dir)
        except Exception:
            pass

def read_proc_stats(pids: List[int]) -> Dict[int, Dict[str, int]]:
    """Returns {pid: {'utime': ..., 'stime': ..., 'vol_cs': ..., 'invol_cs': ...}}"""
    stats = {}
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", "r") as f:
                fields = f.read().split()
                # fields[13]=utime, fields[14]=stime
                utime = int(fields[13])
                stime = int(fields[14])
            
            vol_cs, nonvol_cs = 0, 0
            with open(f"/proc/{pid}/status", "r") as f:
                for line in f:
                    if line.startswith("voluntary_ctxt_switches:"):
                        vol_cs = int(line.split()[1])
                    elif line.startswith("nonvoluntary_ctxt_switches:"):
                        nonvol_cs = int(line.split()[1])

            stats[pid] = {
                "utime": utime,
                "stime": stime,
                "vol_cs": vol_cs,
                "invol_cs": nonvol_cs,
            }
        except Exception:
            pass
    return stats

def sample_metrics(pids: List[int], duration: float) -> Tuple[float, float, float]:
    """Measures total CPU %, vol context switches/sec, invol context switches/sec across pids."""
    clk_tck = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
    s1 = read_proc_stats(pids)
    t1 = time.perf_counter()
    time.sleep(duration)
    s2 = read_proc_stats(pids)
    t2 = time.perf_counter()
    dt = t2 - t1

    total_ticks = 0
    total_vol_cs = 0
    total_invol_cs = 0

    for pid in pids:
        if pid in s1 and pid in s2:
            d_utime = s2[pid]["utime"] - s1[pid]["utime"]
            d_stime = s2[pid]["stime"] - s1[pid]["stime"]
            total_ticks += (d_utime + d_stime)
            total_vol_cs += (s2[pid]["vol_cs"] - s1[pid]["vol_cs"])
            total_invol_cs += (s2[pid]["invol_cs"] - s1[pid]["invol_cs"])

    cpu_seconds = total_ticks / clk_tck
    cpu_percent = (cpu_seconds / dt) * 100.0
    vol_cs_rate = total_vol_cs / dt
    invol_cs_rate = total_invol_cs / dt

    return cpu_percent, vol_cs_rate, invol_cs_rate

def main():
    print(f"=== Starting Local Density Benchmark ({NUM_SANDBOXES} sandboxes) ===")
    os.makedirs(CGROUP_BASE, exist_ok=True)
    os.makedirs(RUNSC_ROOT, exist_ok=True)

    sandboxes: List[Sandbox] = []
    try:
        print(f"1. Spawning {NUM_SANDBOXES} idle gVisor sandboxes...")
        t_spawn_start = time.perf_counter()
        for i in range(1, NUM_SANDBOXES + 1):
            sb = Sandbox(i)
            sb.create_and_start()
            sandboxes.append(sb)
            if i % 10 == 0:
                print(f"   Created {i}/{NUM_SANDBOXES} sandboxes (latest PID: {sb.pid})...")
        t_spawn_end = time.perf_counter()
        print(f"   All {NUM_SANDBOXES} sandboxes running. Spawn duration: {t_spawn_end - t_spawn_start:.2f}s")

        pids = [sb.pid for sb in sandboxes]
        print(f"   Tracking {len(pids)} Sentry processes.")

        # Baseline Measurement
        print(f"\n2. Measuring BASELINE (Unfrozen, Idle) across {SAMPLE_DURATION_SEC}s...")
        cpu_pct_base, vol_base, invol_base = sample_metrics(pids, SAMPLE_DURATION_SEC)
        total_cs_base = vol_base + invol_base
        print(f"   BASELINE RESULTS:")
        print(f"   - Cumulative Sentry CPU: {cpu_pct_base:.3f}% (across {NUM_SANDBOXES} sandboxes)")
        print(f"   - Voluntary CS/sec:     {vol_base:.1f} cs/s")
        print(f"   - Involuntary CS/sec:   {invol_base:.1f} cs/s")
        print(f"   - Total CS/sec:         {total_cs_base:.1f} cs/s")

        # Guest clock check before freeze
        print(f"\n3. Checking initial guest clock on sandbox 1...")
        clk_pre = sandboxes[0].read_guest_clock()
        print(f"   Pre-freeze clock: {clk_pre}")

        # Freezing
        print(f"\n4. Executing Two-Phase Freeze (Approach A + cgroup.freeze) on all {NUM_SANDBOXES} sandboxes...")
        freeze_latencies = []
        t_freeze_all_start = time.perf_counter()
        for sb in sandboxes:
            lat = sb.freeze()
            freeze_latencies.append(lat)
        t_freeze_all_end = time.perf_counter()
        
        freeze_latencies.sort()
        p50_f = statistics.median(freeze_latencies)
        p90_f = freeze_latencies[int(len(freeze_latencies) * 0.90)]
        p99_f = freeze_latencies[-1]
        mean_f = statistics.mean(freeze_latencies)
        print(f"   FREEZE COMPLETED in {t_freeze_all_end - t_freeze_all_start:.3f}s total.")
        print(f"   Per-sandbox Freeze Latency (Pause URPC + cgroup write + poll):")
        print(f"   - Mean: {mean_f:.3f} ms | p50: {p50_f:.3f} ms | p90: {p90_f:.3f} ms | p99: {p99_f:.3f} ms")

        # Frozen Measurement
        print(f"\n5. Measuring FROZEN STATE across {SAMPLE_DURATION_SEC}s...")
        cpu_pct_frozen, vol_frozen, invol_frozen = sample_metrics(pids, SAMPLE_DURATION_SEC)
        total_cs_frozen = vol_frozen + invol_frozen
        print(f"   FROZEN STATE RESULTS:")
        print(f"   - Cumulative Sentry CPU: {cpu_pct_frozen:.3f}%")
        print(f"   - Voluntary CS/sec:     {vol_frozen:.1f} cs/s")
        print(f"   - Involuntary CS/sec:   {invol_frozen:.1f} cs/s")
        print(f"   - Total CS/sec:         {total_cs_frozen:.1f} cs/s")

        # Thawing
        print(f"\n6. Executing Two-Phase Thaw (cgroup.freeze=0 + Approach A Resume) on all {NUM_SANDBOXES} sandboxes...")
        cgroup_thaw_latencies = []
        total_thaw_latencies = []
        t_thaw_all_start = time.perf_counter()
        for sb in sandboxes:
            cg_lat, tot_lat = sb.thaw()
            cgroup_thaw_latencies.append(cg_lat)
            total_thaw_latencies.append(tot_lat)
        t_thaw_all_end = time.perf_counter()

        total_thaw_latencies.sort()
        cgroup_thaw_latencies.sort()
        p50_cg = statistics.median(cgroup_thaw_latencies)
        p90_cg = cgroup_thaw_latencies[int(len(cgroup_thaw_latencies) * 0.90)]
        p99_cg = cgroup_thaw_latencies[-1]
        mean_cg = statistics.mean(cgroup_thaw_latencies)

        p50_t = statistics.median(total_thaw_latencies)
        p90_t = total_thaw_latencies[int(len(total_thaw_latencies) * 0.90)]
        p99_t = total_thaw_latencies[-1]
        mean_t = statistics.mean(total_thaw_latencies)

        print(f"   THAW COMPLETED in {t_thaw_all_end - t_thaw_all_start:.3f}s total.")
        print(f"   Cgroup v2 Thaw Latency (Pre-opened FD write '0' + poll 'frozen 0'):")
        print(f"   - Mean: {mean_cg:.3f} ms | p50: {p50_cg:.3f} ms | p90: {p90_cg:.3f} ms | p99: {p99_cg:.3f} ms")
        print(f"   Total Unfreeze Latency (Cgroup Thaw + Approach A Resume URPC):")
        print(f"   - Mean: {mean_t:.3f} ms | p50: {p50_t:.3f} ms | p90: {p90_t:.3f} ms | p99: {p99_t:.3f} ms")

        # Guest clock check after thaw
        print(f"\n7. Checking post-thaw guest clock on sandbox 1...")
        clk_post = sandboxes[0].read_guest_clock()
        print(f"   Post-thaw clock: {clk_post}")
        
        d_real_sec = (clk_post["REAL_NS"] - clk_pre["REAL_NS"]) / 1e9
        d_mono_sec = (clk_post["MONO_NS"] - clk_pre["MONO_NS"]) / 1e9
        print(f"   Host Realtime Elapsed:    {d_real_sec:.3f} seconds")
        print(f"   Guest Monotonic Elapsed: {d_mono_sec:.3f} seconds")
        print(f"   Monotonic Drift Window:   {d_real_sec - d_mono_sec:.3f} seconds FROZEN")

        # Summary
        print("\n================== BENCHMARK SUMMARY ==================")
        print(f"Sandboxes: {NUM_SANDBOXES}")
        print(f"Baseline CPU:               {cpu_pct_base:.3f}% ({total_cs_base:.1f} CS/sec)")
        print(f"Frozen CPU:                 {cpu_pct_frozen:.3f}% ({total_cs_frozen:.1f} CS/sec)")
        print(f"CPU Reduction:              {((cpu_pct_base - cpu_pct_frozen)/max(cpu_pct_base, 0.001))*100.0:.1f}%")
        print(f"Context Switch Drop:        {((total_cs_base - total_cs_frozen)/max(total_cs_base, 0.001))*100.0:.1f}%")
        print(f"Pre-opened Cgroup Thaw p50: {p50_cg:.3f} ms (p90: {p90_cg:.3f} ms, p99: {p99_cg:.3f} ms)")
        print(f"Total Two-Phase Resume p50: {p50_t:.3f} ms (p90: {p90_t:.3f} ms, p99: {p99_t:.3f} ms)")
        print("=======================================================")

        # Write results json
        summary = {
            "num_sandboxes": NUM_SANDBOXES,
            "sample_duration_sec": SAMPLE_DURATION_SEC,
            "baseline": {
                "cpu_percent": cpu_pct_base,
                "vol_cs_rate": vol_base,
                "invol_cs_rate": invol_base,
                "total_cs_rate": total_cs_base
            },
            "frozen": {
                "cpu_percent": cpu_pct_frozen,
                "vol_cs_rate": vol_frozen,
                "invol_cs_rate": invol_frozen,
                "total_cs_rate": total_cs_frozen
            },
            "freeze_latency_ms": {
                "mean": mean_f, "p50": p50_f, "p90": p90_f, "p99": p99_f
            },
            "cgroup_thaw_latency_ms": {
                "mean": mean_cg, "p50": p50_cg, "p90": p90_cg, "p99": p99_cg
            },
            "total_unfreeze_latency_ms": {
                "mean": mean_t, "p50": p50_t, "p90": p90_t, "p99": p99_t
            },
            "clock_drift": {
                "real_elapsed_sec": d_real_sec,
                "mono_elapsed_sec": d_mono_sec,
                "frozen_window_sec": d_real_sec - d_mono_sec
            }
        }
        with open("/tmp/density_test/benchmark_results.json", "w") as f:
            json.dump(summary, f, indent=2)
        print("Wrote results to /tmp/density_test/benchmark_results.json")

    finally:
        print("\n8. Cleaning up sandboxes and cgroups...")
        for sb in sandboxes:
            sb.cleanup()
        try:
            os.rmdir(CGROUP_BASE)
        except Exception:
            pass
        print("Cleanup completed.")

if __name__ == "__main__":
    main()
