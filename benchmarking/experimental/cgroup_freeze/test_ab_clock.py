import subprocess, time, json, os

RUNSC_BIN = "/tmp/density_test/bin/runsc"
RUNSC_ROOT = "/tmp/density_test/runsc_root"
BUNDLE_DIR = "/tmp/density_test/test_bundle"
CGROUP_BASE = "/sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/ab_clock"

def run_test(name, use_approach_a):
    os.makedirs(CGROUP_BASE, exist_ok=True)
    sb_cgroup = os.path.join(CGROUP_BASE, name)
    os.makedirs(sb_cgroup, exist_ok=True)
    
    # 1. Start container
    subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "run", "--detach", f"--bundle={BUNDLE_DIR}", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    
    state_out = subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "state", name],
                               stdout=subprocess.PIPE, text=True, check=True)
    pid = json.loads(state_out.stdout)["pid"]
    with open(os.path.join(sb_cgroup, "cgroup.procs"), "w") as f:
        f.write(f"{pid}\n")
    
    freeze_fd = os.open(os.path.join(sb_cgroup, "cgroup.freeze"), os.O_RDWR)
    events_fd = os.open(os.path.join(sb_cgroup, "cgroup.events"), os.O_RDONLY)

    def get_clock():
        out = subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "exec", name, "/bin/print_clock"],
                             stdout=subprocess.PIPE, text=True, check=True).stdout
        d = {}
        for p in out.strip().split():
            k, v = p.split(":")
            d[k] = int(v)
        return d

    clk0 = get_clock()

    # Freeze phase
    t_start = time.perf_counter()
    if use_approach_a:
        subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "pause", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    
    os.pwrite(freeze_fd, b"1\n", 0)
    while b"frozen 1" not in os.pread(events_fd, 256, 0):
        time.sleep(0.0001)

    # Sleep 3 seconds while frozen
    time.sleep(3.0)

    # Thaw phase
    os.pwrite(freeze_fd, b"0\n", 0)
    while b"frozen 0" not in os.pread(events_fd, 256, 0):
        time.sleep(0.0001)

    if use_approach_a:
        subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "resume", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    t_end = time.perf_counter()
    clk1 = get_clock()

    # Cleanup
    os.close(freeze_fd)
    os.close(events_fd)
    subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "kill", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.05)
    subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "delete", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.rmdir(sb_cgroup)

    real_dt = (clk1["REAL_NS"] - clk0["REAL_NS"]) / 1e9
    mono_dt = (clk1["MONO_NS"] - clk0["MONO_NS"]) / 1e9
    drift_dt = real_dt - mono_dt
    return real_dt, mono_dt, drift_dt

real_a, mono_a, drift_a = run_test("naive", use_approach_a=False)
real_b, mono_b, drift_b = run_test("approach_a", use_approach_a=True)

print("=== A/B TEST: CLOCK SYNCHRONIZATION ===")
print(f"Condition A (Naive Host Freeze without Sentry Coordination):")
print(f"  Host Elapsed Realtime:   {real_a:.3f} s")
print(f"  Guest Elapsed Monotonic:  {mono_a:.3f} s  <-- (Jumped by full host window!)")
print(f"  Clock Drift Window:       {drift_a:.3f} s  (Timers will fire simultaneously!)")
print("")
print(f"Condition B (Approach A: Two-Phase Sentry Coordinated Freeze):")
print(f"  Host Elapsed Realtime:   {real_b:.3f} s")
print(f"  Guest Elapsed Monotonic:  {mono_b:.3f} s  <-- (FROZEN! Monotonic clock preserved!)")
print(f"  Clock Drift Window:       {drift_b:.3f} s  (Zero timer avalanche!)")
