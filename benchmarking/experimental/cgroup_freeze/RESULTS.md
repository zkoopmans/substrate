# gVisor Local Pause/Resume A/B Benchmark Report: Status Quo vs. Two-Phase Cgroup v2 Freeze

**Cluster Environment:** `substrate-poc` (`zkoopmans-gke-dev`, `us-central1-a`, 2 × `c3-standard-4` nodes, gVisor sandbox runtime)  
**Branch / Commit:** `github/freeze-gvisor` (`8ac1a8e0`)  
**GCS Artifacts:**
- Raw Run Logs, Traces, Phase Breakdowns, & Server Summaries: `gs://snapshot-substrate-test-zkoopmans-gke-dev/benchmarks/runs/`
- Aggregated Prometheus CPU/Memory/Disk Resource Telemetry: `gs://snapshot-substrate-test-zkoopmans-gke-dev/benchmarks/ab_resource_telemetry.json` (`/tmp/ab_resource_telemetry.json` on `duppy`)

---

## 1. Executive Summary

Both conditions execute the **exact same client workload (`glutton.py`) and gRPC API operations** (`SuspendActor(DURABILITY_MODE_ASYNCHRONOUS)` and `ResumeActor`) with **0% failure rate** across all runs:
- **Condition A — Status Quo (`ATE_GVISOR_FREEZE=false`)**: `Atelet.Checkpoint` (`UPLOAD_MODE_DEFERRED`) invokes `runsc checkpoint` to serialize sandbox memory (`pages.img`) to local disk and tears down the sandbox; `Atelet.Restore` creates a fresh sandbox and runs `runsc restore`.
- **Condition B — Frozen Solution (`ATE_GVISOR_FREEZE=true`)**: `Atelet.Checkpoint` (`UPLOAD_MODE_DEFERRED`) invokes `FreezeWorkload` (`containerManager.Pause` over sandbox UDS + `cgroup.freeze = 1` + `memory.low = 40 MiB`); `Atelet.Restore` executes the in-place `ensureThawed` fast-path (`cgroup.freeze = 0` + `containerManager.Resume` over UDS + `/readyz` wakeup probe).

> [!IMPORTANT]
> **Headline Empirical Improvements (Condition B vs. Condition A):**
> - **Up to 369× faster `ResumeActor` latency** (`4,800 ms` → **`13 ms` p50** on a 1 GiB actor; `490 ms` → **`13 ms` p50** at 10 concurrent users).
> - **Up to 372× faster `SuspendActorAsync` latency** (`4,100 ms` → **`11 ms` p50** on a 1 GiB actor; `170 ms` → **`10 ms` p50** at 10 concurrent users).
> - **8.6× lower Worker CPU usage (`14.9×` lower CPU per pause/resume cycle)** under 10-user load (`0.992 cores` → **`0.115 cores` p50**, peak `1.438 cores` → **`0.181 cores`**) while completing **1.73× more cycles** (`223` → `385`).
> - **42.7× reduction in peak disk write bandwidth** on 1 GiB actors (`73.88 MiB/s` → **`1.73 MiB/s`**), eliminating local checkpoint disk I/O wear and page-cache pollution.
> - **31×–41× faster post-resume memory access (`GluttonReadRAM`)** (`400 ms` → **`10 ms` p50** for 1 GiB page walk; `290 ms` → **`7 ms` p50** for 512 MiB) because working-set pages remain warm in RAM.

---

## 2. Client-Perceived Latency & Throughput Comparison

| Scenario | Metric | Condition A: Status Quo (`runsc checkpoint/restore`) | Condition B: Frozen Solution (`FreezeWorkload/ThawWorkload`) | Improvement (p50) |
| :--- | :--- | :---: | :---: | :---: |
| **`glutton_1_user`**<br>*(1 VU, 45s)* | `ResumeActor` (p50 / p95 / p99) | 440 ms / 520 ms / 530 ms (`n=26`) | **14 ms / 17 ms / 20 ms** (`n=41`) | **31.4× faster** |
| | `SuspendActorAsync` (p50 / p95 / p99) | 140 ms / 240 ms / 260 ms (`n=27`) | **11 ms / 21 ms / 110 ms** (`n=42`) | **12.7× faster** |
| | `GluttonPing` (p50 / p95 / p99) | 85 ms / 91 ms / 92 ms (`n=27`) | **5 ms / 7 ms / 93 ms** (`n=42`) | **17.0× faster** |
| **`glutton_5_users`**<br>*(5 VUs, 45s)* | `ResumeActor` (p50 / p95 / p99) | 470 ms / 540 ms / 620 ms (`n=119`) | **13 ms / 17 ms / 22 ms** (`n=199`) | **36.2× faster** |
| | `SuspendActorAsync` (p50 / p95 / p99) | 170 ms / 270 ms / 320 ms (`n=124`) | **10 ms / 27 ms / 73 ms** (`n=204`) | **17.0× faster** |
| | `GluttonPing` (p50 / p95 / p99) | 13 ms / 93 ms / 96 ms (`n=124`) | **4 ms / 10 ms / 93 ms** (`n=204`) | **9.3× faster (p95)** |
| **`glutton_10_users`**<br>*(10 VUs, 45s)* | `ResumeActor` (p50 / p95 / p99) | 490 ms / 540 ms / 600 ms (`n=223`) | **13 ms / 20 ms / 26 ms** (`n=385`) | **37.7× faster** |
| | `SuspendActorAsync` (p50 / p95 / p99) | 170 ms / 280 ms / 310 ms (`n=231`) | **10 ms / 42 ms / 100 ms** (`n=394`) | **17.0× faster** |
| | `GluttonPing` (p50 / p95 / p99) | 79 ms / 93 ms / 96 ms (`n=232`) | **4 ms / 9 ms / 94 ms** (`n=394`) | **19.8× faster** |
| **`glutton_mem_512mi_pause`**<br>*(512 MiB working set, 64 MiB churn, 45s)* | `ResumeActor` (p50 / p95 / p99) | 2,600 ms / 2,600 ms / 2,600 ms (`n=5`) | **13 ms / 19 ms / 28 ms** (`n=21`) | **200.0× faster** |
| | `SuspendActorAsync` (p50 / p95 / p99) | 2,100 ms / 2,300 ms / 2,300 ms (`n=6`) | **12 ms / 110 ms / 110 ms** (`n=22`) | **175.0× faster** |
| | `GluttonReadRAM` (512 MiB walk) | 290 ms / 300 ms / 300 ms (`n=6`) | **7 ms / 10 ms / 200 ms** (`n=22`) | **41.4× faster** |
| **`glutton_mem_1gi_pause`**<br>*(1 GiB working set, 64 MiB churn, 45s)* | `ResumeActor` (p50 / p95 / p99) | 4,800 ms / 4,800 ms / 4,800 ms (`n=2`) | **13 ms / 19 ms / 19 ms** (`n=17`) | **369.2× faster** |
| | `SuspendActorAsync` (p50 / p95 / p99) | 4,100 ms / 7,300 ms / 7,300 ms (`n=3`) | **11 ms / 16 ms / 16 ms** (`n=18`) | **372.7× faster** |
| | `GluttonReadRAM` (1 GiB walk) | 400 ms / 400 ms / 400 ms (`n=3`) | **10 ms / 290 ms / 290 ms** (`n=18`) | **40.0× faster** |

---

## 3. CPU, Memory, and Disk I/O Resource Consumption (`cAdvisor` / Prometheus)

All values below are measured over the steady-state benchmark window via `prometheus.benchmarking.svc.cluster.local:9090` scraping kubelet `/metrics/cadvisor` across the 2 `c3-standard-4` nodes:

| Scenario | Resource Metric | Condition A: Status Quo (avg / p50 / p95) | Condition B: Frozen Solution (avg / p50 / p95) | Impact |
| :--- | :--- | :---: | :---: | :--- |
| **`glutton_1_user`** | **Worker CPU (`cores`)** | `0.088` / `0.118` / `0.147` | **`0.014` / `0.013` / `0.016`** | **9.0× lower Worker CPU (p50)** (`14.2×` lower CPU/cycle) |
| | **Total Node CPU (`cores`)** | `0.680` / `0.713` / `0.841` | **`0.444` / `0.466` / `0.510`** | **35% lower total cluster CPU** |
| | **Node Disk Write (`MiB/s`)** | `14.76` / `15.48` / `22.30` | **`1.89` / `2.07` / `2.31`** | **7.5× lower disk write bandwidth** |
| | **Worker RSS (`MiB`)** | `73.6` / `78.1` / `86.4` | `75.0` / `76.9` / `77.7` | Identical (`~77 MiB` RSS) |
| **`glutton_10_users`** | **Worker CPU (`cores`)** | `0.770` / `0.992` / `1.438` | **`0.090` / `0.115` / `0.181`** | **8.6× lower Worker CPU (p50)** (`14.9×` lower CPU/cycle) |
| | **Total Node CPU (`cores`)** | `1.640` / `1.971` / `2.387` | **`0.766` / `0.846` / `0.871`** | **2.3× lower Node CPU (p50)**, **2.7× lower peak** |
| | **Atelet CPU (`cores`)** | `0.042` / `0.058` / `0.068` | **`0.022` / `0.031` / `0.038`** | **1.9× lower Atelet CPU** despite 1.73× more cycles |
| | **Node Disk Write (`MiB/s`)** | `4.43` / `5.44` / `6.23` | **`3.13` / `3.67` / `4.08`** | **33% lower disk write bandwidth** |
| | **Worker RSS (`MiB`)** | `97.5` / `106.1` / `130.3` | `184.4` / `227.7` / `231.0` | `+121 MiB` RSS for keeping 10 sandboxes resident (`~12 MiB`/actor) |
| | **Total Node Working Set (`MiB`)** | `6,939` / `7,400` / `7,511` | **`6,214` / `6,269` / `6,306`** | **1.13 GiB lower Node working set** (no `pages.img` page-cache bloat) |
| **`glutton_mem_512mi_pause`** | **Worker CPU (`cores`)** | `0.102` / `0.152` / `0.191` (`n=5`) | **`0.078` / `0.094` / `0.130`** (`n=21`) | **38% lower Worker CPU** while doing **4.2× more 512 MiB cycles** |
| | **Node Disk Write (`MiB/s`)** | `5.24` / `4.36` / `9.34` | **`1.67` / `1.67` / `1.88`** | **5.0× lower peak disk write bandwidth** |
| | **Worker Working Set (`MiB`)** | `335.5` / `542.2` / `638.8` | `1,550.5` / `1,677.1` / `1,679.3` | Holds warm 512 MiB Sentry `memfd` in RAM across pauses |
| **`glutton_mem_1gi_pause`** | **Worker CPU (`cores`)** | `0.131` / `0.172` / `0.198` (`n=2`) | **`0.071` / `0.092` / `0.168`** (`n=17`) | **47% lower Worker CPU** while doing **8.5× more 1 GiB cycles** |
| | **Node Disk Write (`MiB/s`)** | `19.98` / `17.09` / **`73.88`** | **`1.54` / `1.52` / `1.73`** | **42.7× lower peak disk write bandwidth** (`73.9` → `1.7 MiB/s`) |
| | **Worker Working Set (`MiB`)** | `440.0` / `481.1` / `726.5` | `1,874.9` / `2,191.1` / `2,192.1` | Holds warm 1 GiB Sentry `memfd` in RAM (`RSS = 78.2 MiB`) |

---

## 4. Node-Level Phase Breakdown (`atelet` & `ateworker-gvisor`)

| Internal Phase (`local` pause/resume) | Metric Name | Baseline (`1_user` → `512Mi` → `1Gi`) | Frozen (`1_user` → `512Mi` → `1Gi`) |
| :--- | :--- | :---: | :---: |
| **`atelet` Checkpoint Total** | `phase_atelet_checkpoint_gvisor_local_none_total` (p50) | `145.8 ms` → `2,084.8 ms` → `3,883.5 ms` | **`1.67 ms` → `2.98 ms` → `1.84 ms`** |
| **`atelet` Restore Total** | `phase_atelet_restore_gvisor_local_none_total` (p50) | `462.9 ms` → `2,526.8 ms` → `4,551.2 ms` | **`3.42 ms` → `3.37 ms` → `3.40 ms`** |
| **`ateworker` Restore Total** | `phase_ateworker_restore_gvisor_local_none_total` (p50) | `460.4 ms` → `2,523.8 ms` → `4,548.2 ms` | **`2.11 ms` → `2.13 ms` → `2.07 ms`** |
| └─ **Thaw / Sandbox+App Restore** | `sandbox_restore` (+ `create`/`app_restore`) (p50) | `370.9 ms` → `2,392.3 ms` → `4,489.3 ms` | **`0.75 ms` → `0.75 ms` → `0.75 ms`** |
| └─ **Wakeup Probe (`/readyz`)** | `wakeup_probe` (p50) | `90.0 ms` → `92.5 ms` → `49.5 ms` | **`1.37 ms` → `1.29 ms` → `1.37 ms`** |

---

## 6. High Actor-Count 3-Way Runtime-Optimized Benchmark Sweep (`25`, `50`, `100`, `250` Actors)

To evaluate higher actor counts with machine types tailored to each runtime's resource profile at matched hourly pool cost:
- **Control Plane & Load Generator Pool (`substrate-node-pool`)**: `2 × c3-standard-4` (hosts `ate-system`, `postgres`, `prometheus`, and `boomer-worker` load generator; isolated from worker pools via `NoSchedule` taints).
- **gVisor Compute-Optimized Worker Pool (`gvisor-pool`)**: `2 × c3-highcpu-8` (`16 vCPUs, 32 GiB RAM` total — `2 GiB/vCPU`, **`$0.68/hr`**).
- **microVM Memory-Optimized Worker Pool (`microvm-pool`)**: `2 × c3-highmem-4` (`8 vCPUs, 64 GiB RAM` total — `8 GiB/vCPU`, nested virtualization enabled, **`$0.59/hr`**).
- **Worker Topology**: 1 `benchmark-worker` pod per node (`replicas: 2`), `ActorTemplate/glutton` sized at `1000m` (`1` vCPU) and `256Mi` memory.
- **Raw Telemetry Archive**: `gs://snapshot-substrate-test-zkoopmans-gke-dev/benchmarks/high_scale_telemetry.json`.

### 6.1 Client Latency & Throughput (`25` to `250` Actors)

| Actor Scale | Runtime & Pool | `ResumeActor` p50 / p95 | `ResumeActorFirstResume` p50 / p95 | `SuspendActorAsync` p50 / p95 | `GluttonPing` p50 / p95 | Completed Cycles (`reqs`) | Cycle Throughput (`rps`) | Failures |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **25 Actors** | **gVisor Baseline** (`2×c3-highcpu-8`) | `180 ms` / `290 ms` | `530 ms` / `810 ms` | `110 ms` / `200 ms` | `9 ms` / `17 ms` | `1,098` | `18.61/s` | `0 (0%)` |
| **25 Actors** | **gVisor Frozen** (`2×c3-highcpu-8`) | **`12 ms` / `16 ms`** | **`220 ms` / `340 ms`** | **`10 ms` / `14 ms`** | **`4 ms` / `8 ms`** | **`1,407`** | **`23.85/s`** | `0 (0%)` |
| **25 Actors** | **microVM** (`2×c3-highmem-4`) | `240 ms` / `450 ms` | `1,600 ms` / `2,600 ms` | `160 ms` / `280 ms` | `13 ms` / `45 ms` | `969` | `16.42/s` | `0 (0%)` |
| **50 Actors** | **gVisor Baseline** (`2×c3-highcpu-8`) | `240 ms` / `410 ms` | `320 ms` / `440 ms` | `160 ms` / `290 ms` | `10 ms` / `26 ms` | `2,004` | `33.97/s` | `0 (0%)` |
| **50 Actors** | **gVisor Frozen** (`2×c3-highcpu-8`) | **`13 ms` / `20 ms`** | **`260 ms` / `360 ms`** | **`11 ms` / `18 ms`** | **`5 ms` / `9 ms`** | **`2,777`** | **`47.07/s`** | `0 (0%)` |
| **50 Actors** | **microVM** (`2×c3-highmem-4`) | `950 ms` / `1,500 ms` | `1,300 ms` / `1,900 ms` | `590 ms` / `1,000 ms` | `72 ms` / `170 ms` | `1,038` | `17.59/s` | `0 (0%)` |
| **100 Actors** | **gVisor Baseline** (`2×c3-highcpu-8`) | `170 ms` / `410 ms` | `370 ms` / `580 ms` | `120 ms` / `310 ms` | `9 ms` / `26 ms` | `1,955` | `33.14/s` | `0 (0%)` |
| **100 Actors** | **gVisor Frozen** (`2×c3-highcpu-8`) | **`11 ms` / `14 ms`** | **`340 ms` / `450 ms`** | **`9 ms` / `14 ms`** | **`4 ms` / `8 ms`** | **`2,213`** | **`36.88/s`** | `0 (0%)` |
| **100 Actors** | **microVM** (`2×c3-highmem-4`) | `230 ms` / `5,100 ms` | `2,400 ms` / `4,600 ms` | `3,000 ms` / `34,000 ms` | `12 ms` / `490 ms` | `322` | `5.46/s` | `0 (0%)` |
| **250 Actors** | **gVisor Baseline** (`2×c3-highcpu-8`) | `140 ms` / `250 ms` | `150 ms` / `310 ms` | `96 ms` / `180 ms` | `8 ms` / `18 ms` | `1,833` | `24.77/s` | `0 (0%)` |
| **250 Actors** | **gVisor Frozen** (`2×c3-highcpu-8`) | **`11 ms` / `15 ms`** | **`140 ms` / `290 ms`** | **`9 ms` / `13 ms`** | **`4 ms` / `9 ms`** | **`2,168`** | **`29.30/s`** | `0 (0%)` |
| **250 Actors** | **microVM** (`2×c3-highmem-4`) | `330 ms` / `2,000 ms` | `1,300 ms` / `17,000 ms` | `390 ms` / `22,000 ms` | `14 ms` / `170 ms` | `202` | `2.73/s` | `0 (0%)` |

### 6.2 Worker Pool CPU, Memory, Disk I/O, and PSI Pressure (`25` to `250` Actors)

*All metrics below are filtered strictly to the 2 worker nodes of the active pool (`cloud_google_com_gke_nodepool="gvisor-pool"` or `"microvm-pool"`).*

| Actor Scale | Runtime & Pool | Worker CPU p50 / p95 (cores) | Total Node CPU p50 / p95 (cores) | Worker RSS p50 (MiB) | Worker Working Set p50 (MiB) | Total Node Mem Working Set p50 (MiB) | Node Disk Write p50 / p95 (MiB/s) | CPU PSI p95 (%) | I/O PSI p95 (%) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **25 Actors** | **gVisor Baseline** | `4.28` / `4.92` | `5.82` / `5.85` | `174 MiB` | `215 MiB` | `3,090 MiB` | `11.32` / `12.82` | `20.2%` | `0.5%` |
| **25 Actors** | **gVisor Frozen** | **`0.23` / `0.36`** | **`0.58` / `0.77`** | `506 MiB` | `712 MiB` | `3,606 MiB` | **`3.11` / `3.41`** | **`2.0%`** | **`0.5%`** |
| **25 Actors** | **microVM** | `1.97` / `3.80` | `4.36` / `4.80` | `43 MiB` | `256 MiB` | `3,547 MiB` | `6.17` / `16.98` | `41.2%` | `1.4%` |
| **50 Actors** | **gVisor Baseline** | `8.20` / `9.58` | `11.40` / `11.64` | `191 MiB` | `250 MiB` | `3,305 MiB` | `21.35` / `22.90` | `49.5%` | `0.6%` |
| **50 Actors** | **gVisor Frozen** | **`0.50` / `1.23`** | **`0.96` / `1.29`** | `953 MiB` | `1,364 MiB` | `4,186 MiB` | **`5.22` / `5.32`** | **`3.7%`** | **`0.4%`** |
| **50 Actors** | **microVM** | `3.87` / `4.62` | `5.60` / `5.68` | `78 MiB` | `1,542 MiB` | `4,698 MiB` | `57.09` / `70.79` | `91.0%` | `7.4%` |
| **100 Actors** | **gVisor Baseline** | `7.50` / `9.61` | `11.34` / `11.61` | `136 MiB` | `196 MiB` | `3,269 MiB` | `22.37` / `23.37` | `46.2%` | `1.0%` |
| **100 Actors** | **gVisor Frozen** | **`0.52` / `1.03`** | **`1.21` / `1.89`** | `1,808 MiB` | `2,502 MiB` | `5,307 MiB` | **`4.98` / `5.63`** | **`6.7%`** | **`0.6%`** |
| **100 Actors** | **microVM** | `1.22` / `2.27` | `1.32` / `3.32` | `46 MiB` | `538 MiB` | `3,350 MiB` (`6,254` p95) | `301.73` / `335.99` | `49.7%` | **`95.6%`** |
| **250 Actors** | **gVisor Baseline** | `7.72` / `7.94` | `9.20` / `9.55` | `117 MiB` | `181 MiB` | `3,158 MiB` | `154.04` / `175.12` | `31.7%` | `5.2%` |
| **250 Actors** | **gVisor Frozen** | **`0.79` / `3.34`** | **`1.30` / `3.48`** | `4,200 MiB` | `5,898 MiB` | `8,460 MiB` | **`4.83` / `9.06`** | **`9.4%`** | **`1.2%`** |
| **250 Actors** | **microVM** | `0.86` / `2.62` | `1.62` / `3.28` | `54 MiB` | `943 MiB` | `3,385 MiB` (`4,303` p95) | `336.00` / `336.01` | `46.6%` | **`99.2%`** |

### 6.3 Architectural & Cost-Efficiency Conclusions

1. **Why microVM Collapses at `100–250` Actors (`95.6%–99.2%` I/O PSI Stall)**:
   - Even on memory-optimized `c3-highmem-4` nodes (`64 GiB RAM`), `cloud-hypervisor` suspend/resume must write a full **`256 MiB` guest physical RAM file (`memory-ranges`)** to disk on every suspend.
   - At `100` and `250` actors, disk write bandwidth saturates the **exact `336 MiB/s` PD-Balanced ceiling** (`2 × 168 MiB/s` across the 2 nodes), driving kernel I/O pressure (`IO_PSI`) to **`95.6%–99.2%`**, stalling `SuspendActorAsync` to **`34,000 ms` p95**, and collapsing throughput to **`2.73–5.46` cycles/s**.
2. **Why gVisor Frozen Scales Linearly at `11–13 ms` with `~16.3 MiB` RSS per Actor**:
   - By freezing the `runsc` sandbox in-place via `cgroup.freeze = 1`, `gvisor_frozen` eliminates checkpoint/restore serialization and disk I/O (`4.83 MiB/s` vs `154.04 MiB/s` on `gvisor_baseline` and `336.00 MiB/s` on `microvm` at 250 actors).
   - At `250` actors across 2 nodes (`125` actors/node), `gvisor_frozen` holds all 250 sandboxes in **`4,200 MiB` of Worker RSS (`~16.3 MiB` per frozen actor above baseline)** and **`8,460 MiB` total Node Working Set** (just **26.4%** of the 32 GiB RAM on `2 × c3-highcpu-8`), while using only **`0.79` Worker CPU cores (`1.30` total Node CPU cores — 8.1% of the 16 vCPUs)**!
3. **Cost per 1 Million Pause/Resume Transitions**:
   - **gVisor Frozen (`2 × c3-highcpu-8`, `$0.68/hr`)**: At `50` actors (`47.07` cycles/s = `169,452` cycles/hr at only 6% node CPU utilization), cost is **`$4.01` per 1M transitions** (and CPU-normalized at 70% utilization, **`~$0.34` per 1M transitions**).
   - **gVisor Baseline (`2 × c3-highcpu-8`, `$0.68/hr`)**: At `50` actors (`33.97` cycles/s at 71% node CPU utilization), cost is **`$5.56` per 1M transitions** (**16.4× higher CPU cost per transition** than Frozen).
   - **microVM (`2 × c3-highmem-4`, `$0.59/hr`)**: Caps out at `17.59` cycles/s at 50 actors (`$9.32` per 1M transitions) and collapses to `2.73` cycles/s at 250 actors (**`$60.03` per 1M transitions**).

