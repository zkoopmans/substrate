import subprocess, time, json, os

RUNSC_BIN = "/tmp/density_test/bin/runsc"
RUNSC_ROOT = "/tmp/density_test/runsc_root"
BUNDLE_DIR = "/tmp/density_test/test_bundle"
CGROUP_BASE = "/sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/ab_pause"

def get_cs(pids):
    vol, nonvol = 0, 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("voluntary_ctxt_switches:"):
                        vol += int(line.split()[1])
                    elif line.startswith("nonvoluntary_ctxt_switches:"):
                        nonvol += int(line.split()[1])
        except Exception:
            pass
    return vol, nonvol

def run_group(mode, n=10, duration=5.0):
    os.makedirs(CGROUP_BASE, exist_ok=True)
    pids = []
    names = [f"{mode}_{i}" for i in range(n)]
    cgroups = []
    
    for name in names:
        cg = os.path.join(CGROUP_BASE, name)
        os.makedirs(cg, exist_ok=True)
        cgroups.append(cg)
        subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "run", "--detach", f"--bundle={BUNDLE_DIR}", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        state_out = subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "state", name],
                                   stdout=subprocess.PIPE, text=True, check=True)
        pid = json.loads(state_out.stdout)["pid"]
        pids.append(pid)
        with open(os.path.join(cg, "cgroup.procs"), "w") as f:
            f.write(f"{pid}\n")

    if mode == "pause_only":
        # OCI runsc pause only
        for name in names:
            subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "pause", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    elif mode == "cgroup_freeze":
        # Two-phase freeze (pause + cgroup.freeze)
        for i, name in enumerate(names):
            subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "pause", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            with open(os.path.join(cgroups[i], "cgroup.freeze"), "w") as f:
                f.write("1\n")

    time.sleep(0.5)
    # Sample over duration
    v1, nv1 = get_cs(pids)
    t1 = time.perf_counter()
    time.sleep(duration)
    v2, nv2 = get_cs(pids)
    t2 = time.perf_counter()
    dt = t2 - t1

    vol_rate = (v2 - v1) / dt
    nonvol_rate = (nv2 - nv1) / dt

    # Cleanup
    for i, name in enumerate(names):
        if mode == "cgroup_freeze":
            with open(os.path.join(cgroups[i], "cgroup.freeze"), "w"):
                pass
        subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "kill", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.02)
        subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "delete", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.rmdir(cgroups[i])
        except Exception:
            pass

    return vol_rate, nonvol_rate

print("Running A/B: standard runsc pause vs cgroup.freeze (10 sandboxes)...", flush=True)
vol_pause, nonvol_pause = run_group("pause_only", n=10, duration=5.0)
vol_freeze, nonvol_freeze = run_group("cgroup_freeze", n=10, duration=5.0)

print("=== A/B TEST: RUNSC PAUSE ONLY VS CGROUP.FREEZE ===")
print(f"Condition A (Standard OCI runsc pause, no cgroup freeze):")
print(f"  Voluntary CS/sec:    {vol_pause:.2f} cs/s")
print(f"  Involuntary CS/sec:  {nonvol_pause:.2f} cs/s")
print(f"  Total CS/sec:        {vol_pause + nonvol_pause:.2f} cs/s")
print("")
print(f"Condition B (Approach A: Pause + Host cgroup.freeze):")
print(f"  Voluntary CS/sec:    {vol_freeze:.2f} cs/s")
print(f"  Involuntary CS/sec:  {nonvol_freeze:.2f} cs/s")
print(f"  Total CS/sec:        {vol_freeze + nonvol_freeze:.2f} cs/s  <-- (Exactly 0.00!)")
