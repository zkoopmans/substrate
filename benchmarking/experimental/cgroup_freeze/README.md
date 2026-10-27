# gVisor Cgroup v2 Freezing Verification & Benchmark Suite

This directory contains the verification harnesses, microbenchmark scripts, and end-to-end GKE cluster benchmark results used to validate Two-Phase Cgroup v2 Freezing for gVisor in Substrate.

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

5. **End-to-End GKE Cluster A/B & 3-Way Runtime-Optimized Benchmark Results (`substrate-poc`):**
   - `RESULTS.md`: Full empirical report comparing gVisor Baseline (`ATE_GVISOR_FREEZE=false`), gVisor Frozen (`ATE_GVISOR_FREEZE=true`), and Cloud Hypervisor microVM (`ateworker-microvm`) across small (`1`, `5`, `10` users, `512Mi`/`1Gi` RAM) and high (`25`, `50`, `100`, `250` actors) scales on runtime-optimized C3 node pools (`2 x c3-highcpu-8` for gVisor vs `2 x c3-highmem-4` for microVM).
   - `ab_resource_telemetry.json`: Raw Prometheus/cAdvisor + Locust telemetry for the small-scale A/B and 512Mi/1Gi memory suites.
   - `high_scale_telemetry.json`: Raw Prometheus/cAdvisor + Locust telemetry for the 3-way runtime-optimized high actor-count sweep (`25`, `50`, `100`, `250` actors).
   - `high_scale_telemetry_50m_10replicas.json`: Raw telemetry from the 10-replica / 50m CPU quota experiment demonstrating why 1 worker pod per node (`replicas: 2`) is required for `AssignedNode` frozen-sandbox affinity.
   - `run_high_scale_sweep.py`: Automated 3-way GKE benchmark sweep and Prometheus telemetry harvesting script.
