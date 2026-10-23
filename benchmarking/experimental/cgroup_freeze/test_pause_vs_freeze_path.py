import subprocess, time, json, os, statistics

RUNSC_BIN = "/tmp/density_test/bin/runsc"
RUNSC_ROOT = "/tmp/density_test/runsc_root"
BUNDLE_DIR = "/tmp/density_test/test_bundle"
CGROUP_DIR = "/sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/path_comparison"

os.makedirs(CGROUP_DIR, exist_ok=True)
NAME = "sb_comp"

# Launch sandbox
subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "run", "--detach", f"--bundle={BUNDLE_DIR}", NAME],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

state_out = subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "state", NAME],
                           stdout=subprocess.PIPE, text=True, check=True)
pid = json.loads(state_out.stdout)["pid"]
with open(os.path.join(CGROUP_DIR, "cgroup.procs"), "w") as f:
    f.write(f"{pid}\n")

freeze_fd = os.open(os.path.join(CGROUP_DIR, "cgroup.freeze"), os.O_RDWR)
events_fd = os.open(os.path.join(CGROUP_DIR, "cgroup.events"), os.O_RDONLY)

def get_proc_state(pid):
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("State:"):
                return line.strip()
    return ""

# Benchmark Path A: Standard Pause / Resume (runsc pause -> runsc resume)
pause_a_lat = []
resume_a_lat = []
state_a = ""

for _ in range(30):
    # Pause
    t0 = time.perf_counter()
    subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "pause", NAME],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    t1 = time.perf_counter()
    pause_a_lat.append((t1 - t0) * 1000.0)

    if not state_a:
        state_a = get_proc_state(pid)

    # Resume
    t2 = time.perf_counter()
    subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "resume", NAME],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    t3 = time.perf_counter()
    resume_a_lat.append((t3 - t2) * 1000.0)

# Benchmark Path B: Our Two-Phase Freeze / Thaw (Pause + cgroup.freeze -> cgroup.thaw + Resume)
freeze_b_lat = []
thaw_b_lat = []
cgroup_freeze_lat = []
cgroup_thaw_lat = []
state_b = ""

for _ in range(30):
    # Freeze
    t0 = time.perf_counter()
    # 1. runsc pause
    subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "pause", NAME],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    t_pause = time.perf_counter()
    # 2. cgroup.freeze = 1 via pre-opened FD
    os.pwrite(freeze_fd, b"1\n", 0)
    while b"frozen 1" not in os.pread(events_fd, 256, 0):
        time.sleep(0.00005)
    t1 = time.perf_counter()
    
    freeze_b_lat.append((t1 - t0) * 1000.0)
    cgroup_freeze_lat.append((t1 - t_pause) * 1000.0)

    if not state_b:
        state_b = get_proc_state(pid)

    # Thaw
    t2 = time.perf_counter()
    # 1. cgroup.freeze = 0 via pre-opened FD
    os.pwrite(freeze_fd, b"0\n", 0)
    while b"frozen 0" not in os.pread(events_fd, 256, 0):
        time.sleep(0.00005)
    t_thaw = time.perf_counter()
    # 2. runsc resume
    subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "resume", NAME],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    t3 = time.perf_counter()

    thaw_b_lat.append((t3 - t2) * 1000.0)
    cgroup_thaw_lat.append((t_thaw - t2) * 1000.0)

# Cleanup
os.close(freeze_fd)
os.close(events_fd)
subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "kill", NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.05)
subprocess.run([RUNSC_BIN, "--rootless", "--network=none", f"--root={RUNSC_ROOT}", "delete", NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os.rmdir(CGROUP_DIR)

print("=== HEAD-TO-HEAD: CURRENT PAUSE/RESUME PATH VS. OUR FREEZE/THAW PATH ===")
print("")
print("1. SUSPENSION / INGRESS PATH:")
print(f"   Current Path (runsc pause only):       p50 = {statistics.median(pause_a_lat):.3f} ms, Mean = {statistics.mean(pause_a_lat):.3f} ms")
print(f"   Our Freeze Path (pause + cgroup freeze): p50 = {statistics.median(freeze_b_lat):.3f} ms, Mean = {statistics.mean(freeze_b_lat):.3f} ms")
print(f"   -> cgroup freeze pre-opened FD portion:  p50 = {statistics.median(cgroup_freeze_lat):.3f} ms ({statistics.median(cgroup_freeze_lat)*1000:.1f} µs)")
print(f"   -> Overhead added by cgroup freeze:      +{statistics.median(freeze_b_lat) - statistics.median(pause_a_lat):.3f} ms")
print(f"   -> Process State in Sched: Current = '{state_a}' vs Freeze = '{state_b}'")
print("")
print("2. ACTIVATION / RESUME PATH:")
print(f"   Current Path (runsc resume only):      p50 = {statistics.median(resume_a_lat):.3f} ms, Mean = {statistics.mean(resume_a_lat):.3f} ms")
print(f"   Our Thaw Path (cgroup thaw + resume):  p50 = {statistics.median(thaw_b_lat):.3f} ms, Mean = {statistics.mean(thaw_b_lat):.3f} ms")
print(f"   -> cgroup thaw pre-opened FD portion:    p50 = {statistics.median(cgroup_thaw_lat):.3f} ms ({statistics.median(cgroup_thaw_lat)*1000:.1f} µs)")
print(f"   -> Overhead added by cgroup thaw:        +{statistics.median(thaw_b_lat) - statistics.median(resume_a_lat):.3f} ms")
