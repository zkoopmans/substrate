import socket
import json
import subprocess
import time
import os
import sys

RUNSC_BIN = '/tmp/density_test/bin/runsc'
RUNSC_ROOT = '/tmp/density_test/runsc_root'
BUNDLE_DIR = '/tmp/density_test/test_bundle'
CGROUP_BASE = '/sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice'
PORT = 39998

def run_test(use_two_phase: bool, freeze_seconds: float = 60.0):
    mode_name = 'Two-Phase Substrate Freeze (Approach A)' if use_two_phase else 'Naive Host Cgroup Freeze (No Sentry Pause)'
    print('=================================================================')
    print('RUNNING: ' + mode_name)
    print('Freeze Duration: %.1fs (TCP ReadDeadline: 5.0s, Systrap Watchdog: 30.0s)' % freeze_seconds)
    print('=================================================================')

    name = 'tcp_sb_' + ('twophase' if use_two_phase else 'naive')
    cg_dir = os.path.join(CGROUP_BASE, name)
    os.makedirs(cg_dir, exist_ok=True)

    # Clean old instance
    subprocess.run([RUNSC_BIN, '--rootless', f'--root={RUNSC_ROOT}', 'kill', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.05)
    subprocess.run([RUNSC_BIN, '--rootless', f'--root={RUNSC_ROOT}', 'delete', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 1. Start sandbox with --platform=systrap
    cmd = [RUNSC_BIN, '--platform=systrap', '--rootless', '--network=host', f'--root={RUNSC_ROOT}',
           'run', '--detach', f'--bundle={BUNDLE_DIR}', name]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # Move PID to cgroup
    state_out = subprocess.run([RUNSC_BIN, '--rootless', f'--root={RUNSC_ROOT}', 'state', name],
                               stdout=subprocess.PIPE, text=True, check=True)
    pid = json.loads(state_out.stdout)['pid']
    with open(os.path.join(cg_dir, 'cgroup.procs'), 'w') as f:
        f.write(str(pid) + '\n')

    # Start TCP deadline server in background inside sandbox
    subprocess.run([RUNSC_BIN, '--rootless', f'--root={RUNSC_ROOT}', 'exec', '--detach', name, '/bin/server', str(PORT)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    time.sleep(1.0)

    # UDS socket connection for Sentry
    sock_path = f'{RUNSC_ROOT}/runsc-{name}.sock'
    sentry_sock = None
    if use_two_phase:
        sentry_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sentry_sock.connect(sock_path)

    # Connect to TCP server on localhost:PORT
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(('127.0.0.1', PORT))
    print('[1] Connected to guest TCP server on 127.0.0.1:%d' % PORT)

    # Send initial greeting
    client.sendall(b'HELLO\n')
    ack = client.recv(1024)
    print('[2] Received greeting ACK from server: %r' % ack.decode().strip())
    print('    (Server has now armed a 5.0s ReadDeadline on this connection)')

    # FREEZE
    t0_freeze = time.perf_counter()
    if use_two_phase:
        print('[3] Initiating Phase 1: Direct UDS containerManager.Pause...')
        sentry_sock.sendall(json.dumps({'method': 'containerManager.Pause', 'arg': None}).encode())
        res_pause = sentry_sock.recv(4096)
        print('    Sentry paused tasks and latched T_freeze: %s' % res_pause.decode().strip())

    print('[4] Writing cgroup.freeze = 1 (Holding frozen for %.1fs)...' % freeze_seconds)
    with open(os.path.join(cg_dir, 'cgroup.freeze'), 'w') as f:
        f.write('1\n')

    while True:
        with open(os.path.join(cg_dir, 'cgroup.events'), 'r') as f:
            if 'frozen 1' in f.read():
                break
        time.sleep(0.001)

    print('    --> Sandbox is now FROZEN. Waiting %.1fs...' % freeze_seconds)
    t_sleep_start = time.perf_counter()
    last_print = 0
    while True:
        elapsed = time.perf_counter() - t_sleep_start
        if elapsed >= freeze_seconds:
            break
        if int(elapsed) // 10 > last_print:
            last_print = int(elapsed) // 10
            print('    ... %ds elapsed in frozen state' % (last_print * 10))
        time.sleep(min(1.0, freeze_seconds - elapsed))

    # THAW
    print('[5] Thawing host cgroup (writing cgroup.freeze = 0)...')
    with open(os.path.join(cg_dir, 'cgroup.freeze'), 'w') as f:
        f.write('0\n')

    while True:
        with open(os.path.join(cg_dir, 'cgroup.events'), 'r') as f:
            if 'frozen 0' in f.read():
                break
        time.sleep(0.001)

    if use_two_phase:
        print('[6] Initiating Phase 2: Direct UDS containerManager.Resume...')
        sentry_sock.sendall(json.dumps({'method': 'containerManager.Resume', 'arg': None}).encode())
        res_resume = sentry_sock.recv(4096)
        print('    Sentry adjusted monotonicOffset & resumed vDSO: %s' % res_resume.decode().strip())

    actual_freeze_wall = time.perf_counter() - t0_freeze
    print('[7] Host dwell duration: %.3fs' % actual_freeze_wall)

    # Send msg2 to server post-thaw
    print('[8] Delivering msg2 (WORLD) over existing TCP connection post-thaw...')
    time.sleep(0.1)
    try:
        client.sendall(b'WORLD\n')
        client.settimeout(5.0)
        resp = client.recv(4096)
        print('[9] Received Server Response:\n    %s' % resp.decode().strip())
    except Exception as e:
        print('[9] FAILED: Connection died or dropped upon thaw: %s' % e)

    # Check Sentry process health
    p_check = subprocess.run([RUNSC_BIN, '--rootless', f'--root={RUNSC_ROOT}', 'state', name],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    print('[10] Post-Thaw Container State: %s' % p_check.stdout.strip())

    # Cleanup
    if sentry_sock:
        sentry_sock.close()
    client.close()
    subprocess.run([RUNSC_BIN, '--rootless', f'--root={RUNSC_ROOT}', 'kill', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.1)
    subprocess.run([RUNSC_BIN, '--rootless', f'--root={RUNSC_ROOT}', 'delete', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.rmdir(cg_dir)
    except Exception:
        pass

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'both'
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    if mode in ('twophase', 'both'):
        run_test(use_two_phase=True, freeze_seconds=dur)
    if mode in ('naive', 'both'):
        run_test(use_two_phase=False, freeze_seconds=dur)
