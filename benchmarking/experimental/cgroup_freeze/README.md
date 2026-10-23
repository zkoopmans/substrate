# gVisor Cgroup v2 Freezing Verification & Benchmark Suite

This directory contains the verification harnesses and microbenchmark scripts used to validate Two-Phase Cgroup v2 Freezing for gVisor in Substrate.

## Contents

1. **Guest Microbenchmarks & Workloads:**
   - `print_clock.c`: Reads `CLOCK_MONOTONIC` and `CLOCK_REALTIME` via `clock_gettime(2)` to detect clock drift across freezes.
   - `ticker_app.c`: Periodic ticker app to detect timer coalescing and timer queue stalls.

2. **Clock and Scheduler Verification:**
   - `test_ab_clock.py`: A/B comparison of monotonic clock drift under naive host cgroup freeze vs Approach A coordinated freeze.
   - `test_ab_pause_vs_freeze.py`: Context switch churn comparison (`TASK_INTERRUPTIBLE` vs `TASK_FROZEN`).
   - `test_pause_vs_freeze_path.py`: Microbenchmark measuring latency breakdown across standard pause/resume vs two-phase freeze/thaw.
   - `test_clock_freeze.sh`: Shell test wrapper for monotonic clock drift validation.
   - `test_cgroup_sb.sh`: Rootless cgroup v2 sandbox creation helper.

3. **IPC Control & TCP Resilience:**
   - `test_uds.py`: Microbenchmark measuring direct Unix domain socket latency for `containerManager.Pause` (0.199 ms) and `Resume` (0.125 ms).
   - `test_uds_freeze.py`: Full two-phase freeze sequence executed over direct Sentry UDS socket.
   - `test_uds_go.go`: Go client implementation for Sentry JSON-RPC control socket.
   - `test_60s_tcp_freeze.py`: 60.0s freeze dwell test verifying guest TCP deadline resilience (5.0s read deadline) and Systrap 30s watchdog bug immunity (Issue #16160).

4. **Density & Fleet Benchmark:**
   - `benchmark_density.py`: 50-sandbox concurrent density harness measuring host CPU utilization, PSI metrics, context switch reduction (0.0 CS/sec), and p50 thaw latency.
