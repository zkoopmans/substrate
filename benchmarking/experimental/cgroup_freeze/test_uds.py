import socket, json, subprocess, time

RUNSC_BIN = '/tmp/density_test/bin/runsc'
RUNSC_ROOT = '/tmp/density_test/runsc_root'
BUNDLE_DIR = '/tmp/density_test/test_bundle'
NAME = 'uds_test'

subprocess.run([RUNSC_BIN, '--rootless', '--network=none', f'--root={RUNSC_ROOT}', 'run', '--detach', f'--bundle={BUNDLE_DIR}', NAME],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

sock_path = f'{RUNSC_ROOT}/runsc-{NAME}.sock'
print(f'Connecting to control socket: {sock_path}')

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock_path)

# Measure direct UDS Pause
t0 = time.perf_counter()
req_pause = json.dumps({'method': 'containerManager.Pause', 'arg': None}).encode()
s.sendall(req_pause)
resp_pause = s.recv(4096)
t1 = time.perf_counter()

print(f'Pause response: {resp_pause.decode()}')
print(f'UDS Pause Latency: {(t1 - t0)*1000:.3f} ms ({(t1 - t0)*1e6:.1f} µs)!')

state_out = subprocess.run([RUNSC_BIN, '--rootless', '--network=none', f'--root={RUNSC_ROOT}', 'state', NAME],
                           stdout=subprocess.PIPE, text=True, check=True)
status_val = json.loads(state_out.stdout)['status']
print(f'Container status after UDS pause: {status_val}')

# Measure direct UDS Resume
t2 = time.perf_counter()
req_resume = json.dumps({'method': 'containerManager.Resume', 'arg': None}).encode()
s.sendall(req_resume)
resp_resume = s.recv(4096)
t3 = time.perf_counter()

print(f'Resume response: {resp_resume.decode()}')
print(f'UDS Resume Latency: {(t3 - t2)*1000:.3f} ms ({(t3 - t2)*1e6:.1f} µs)!')

state_out2 = subprocess.run([RUNSC_BIN, '--rootless', '--network=none', f'--root={RUNSC_ROOT}', 'state', NAME],
                            stdout=subprocess.PIPE, text=True, check=True)
status_val2 = json.loads(state_out2.stdout)['status']
print(f'Container status after UDS resume: {status_val2}')

s.close()
subprocess.run([RUNSC_BIN, '--rootless', '--network=none', f'--root={RUNSC_ROOT}', 'kill', NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(0.05)
subprocess.run([RUNSC_BIN, '--rootless', '--network=none', f'--root={RUNSC_ROOT}', 'delete', NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
