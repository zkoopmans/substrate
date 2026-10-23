import socket, json, subprocess, time, os

RUNSC_BIN = '/tmp/density_test/bin/runsc'
RUNSC_ROOT = '/tmp/density_test/runsc_root'
BUNDLE_DIR = '/tmp/density_test/test_bundle'
CGROUP_DIR = '/sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/uds_freeze'
NAME = 'uds_sb'

os.makedirs(CGROUP_DIR, exist_ok=True)
freeze_fd = os.open(os.path.join(CGROUP_DIR, 'cgroup.freeze'), os.O_RDWR)
events_fd = os.open(os.path.join(CGROUP_DIR, 'cgroup.events'), os.O_RDONLY)

subprocess.run([RUNSC_BIN, '--rootless', '--network=none', f'--root={RUNSC_ROOT}', 'run', '--detach', f'--bundle={BUNDLE_DIR}', NAME],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

state_out = subprocess.run([RUNSC_BIN, '--rootless', '--network=none', f'--root={RUNSC_ROOT}', 'state', NAME],
                           stdout=subprocess.PIPE, text=True, check=True)
pid = json.loads(state_out.stdout)['pid']
with open(os.path.join(CGROUP_DIR, 'cgroup.procs'), 'w') as f:
    f.write(f'{pid}\n')

sock_path = f'{RUNSC_ROOT}/runsc-{NAME}.sock'
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock_path)

def get_guest_clock():
    out = subprocess.run([RUNSC_BIN, '--rootless', '--network=none', f'--root={RUNSC_ROOT}', 'exec', NAME, '/bin/print_clock'],
                         stdout=subprocess.PIPE, text=True, check=True).stdout
    d = {}
    for p in out.strip().split():
        k, v = p.split(':')
        d[k] = int(v)
    return d

clk0 = get_guest_clock()

# Direct UDS Two-Phase Freeze
t0 = time.perf_counter()
s.sendall(json.dumps({'method': 'containerManager.Pause', 'arg': None}).encode())
resp1 = s.recv(4096)
t_pause = time.perf_counter()

os.pwrite(freeze_fd, b'1\n', 0)
while b'frozen 1' not in os.pread(events_fd, 256, 0):
    time.sleep(0.00005)
t1 = time.perf_counter()

total_freeze_ms = (t1 - t0) * 1000.0
uds_pause_ms = (t_pause - t0) * 1000.0
cgroup_freeze_ms = (t1 - t_pause) * 1000.0

# Sleep 3 seconds while frozen
time.sleep(3.0)

# Direct UDS Two-Phase Thaw
t2 = time.perf_counter()
os.pwrite(freeze_fd, b'0\n', 0)
while b'frozen 0' not in os.pread(events_fd, 256, 0):
    time.sleep(0.00005)
t_thaw = time.perf_counter()

s.sendall(json.dumps({'method': 'containerManager.Resume', 'arg': None}).encode())
resp2 = s.recv(4096)
t3 = time.perf_counter()

total_thaw_ms = (t3 - t2) * 1000.0
cgroup_thaw_ms = (t_thaw - t2) * 1000.0
uds_resume_ms = (t3 - t_thaw) * 1000.0

clk1 = get_guest_clock()

real_dt = (clk1['REAL_NS'] - clk0['REAL_NS']) / 1e9
mono_dt = (clk1['MONO_NS'] - clk0['MONO_NS']) / 1e9

s.close()
os.close(freeze_fd)
os.close(events_fd)
subprocess.run([RUNSC_BIN, '--rootless', '--network=none', f'--root={RUNSC_ROOT}', 'kill', NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.05)
subprocess.run([RUNSC_BIN, '--rootless', '--network=none', f'--root={RUNSC_ROOT}', 'delete', NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os.rmdir(CGROUP_DIR)

print('=== UDS + CGROUP FREEZE RESULTS ===')
print(f'Total Freeze Latency: {total_freeze_ms:.3f} ms (UDS Pause: {uds_pause_ms:.3f} ms, cgroup write: {cgroup_freeze_ms:.3f} ms)')
print(f'Total Thaw Latency:   {total_thaw_ms:.3f} ms (cgroup thaw: {cgroup_thaw_ms:.3f} ms, UDS Resume: {uds_resume_ms:.3f} ms)')
print(f'Host Real Elapsed:    {real_dt:.3f} s')
print(f'Guest Mono Elapsed:   {mono_dt:.3f} s')
print(f'Frozen Drift Window:  {real_dt - mono_dt:.3f} s FROZEN')
