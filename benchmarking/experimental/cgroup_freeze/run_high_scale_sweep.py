import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
import yaml

REPO = Path('/usr/local/google/home/zkoopmans/gke-internal-kubernetes/src/k8s.io/substrate')
TMPL = REPO / 'benchmarking/automation/manifests/runner-job.yaml.tmpl'
IMAGE = 'gcr.io/zkoopmans-gke-dev/ate-images/locust-test:latest'
DEST = 'gs://snapshot-substrate-test-zkoopmans-gke-dev/benchmarks'
OUT_JSON = Path('/tmp/high_scale_telemetry.json')

TESTS = [
    ('glutton_25_actors', 25, 60, ['-r', '15', '--min-wait-time', '1.0', '--max-wait-time', '1.0', '--trace-probability', '0.2', '--atelet-lag-s', '10', '--no-phase-breakdown']),
    ('glutton_50_actors', 50, 60, ['-r', '20', '--min-wait-time', '1.0', '--max-wait-time', '1.0', '--trace-probability', '0.1', '--atelet-lag-s', '10', '--no-phase-breakdown']),
    ('glutton_100_actors', 100, 60, ['-r', '25', '--min-wait-time', '2.0', '--max-wait-time', '3.0', '--trace-probability', '0.05', '--atelet-lag-s', '10', '--no-phase-breakdown']),
    ('glutton_250_actors', 50, 75, ['-r', '20', '--actors-per-user', '5', '--min-wait-time', '1.0', '--max-wait-time', '2.0', '--trace-probability', '0.05', '--atelet-lag-s', '10', '--no-phase-breakdown']),
]

def sh(cmd: list[str], check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, input=input_text, capture_output=True)

def cleanup_actors():
    for ns in ('benchmark-workloads', 'benchmark'):
        out = sh(['/tmp/kubectl-ate', 'get', 'actor', '-a', ns, '-o', 'json'], check=False)
        if out.returncode == 0 and out.stdout.strip():
            try:
                data = json.loads(out.stdout)
                items = data if isinstance(data, list) else data.get('items', data.get('actors', []))
                for item in items:
                    name = item.get('metadata', {}).get('name')
                    if name:
                        sh(['/tmp/kubectl-ate', 'delete', 'actor', name, '-a', ns], check=False)
            except Exception:
                pass

def recreate_glutton_template():
    print('=== Recreating glutton ActorTemplate (cpuMillicores=1000, memoryBytes=256Mi) ===', flush=True)
    sh(['/tmp/kubectl-ate', 'delete', 'actor-template', 'glutton', '-a', 'benchmark-workloads'], check=False)
    otlp = sh(['kubectl', 'get', 'configmap', 'ate-otel-config', '-n', 'ate-system', '-o', 'jsonpath={.data.OTEL_EXPORTER_OTLP_ENDPOINT}']).stdout.strip()
    tmpl_text = (REPO / 'benchmarking/workloads/manifests/glutton-template.yaml.tmpl').read_text()
    tmpl_text = (
        tmpl_text
        .replace('${BUCKET_NAME}', 'snapshot-substrate-test-zkoopmans-gke-dev')
        .replace('${OTLP_ENDPOINT}', otlp)
        .replace('${ACTOR_CPU_MILLICORES}', '1000')
        .replace('${ACTOR_MEMORY_BYTES}', '268435456')
    )
    resolved = subprocess.run(
        'source .ate-dev-env.sh && ./hack/run-tool.sh ko resolve -f -',
        shell=True, cwd=str(REPO), executable='/bin/bash', input=tmpl_text, check=True, text=True, capture_output=True
    ).stdout
    sh(['/tmp/kubectl-ate', 'create', 'actor-template', '-f', '-'], input_text=resolved)
    deadline = time.time() + 300
    while time.time() < deadline:
        p = sh(['/tmp/kubectl-ate', 'get', 'actor-template', 'glutton', '-a', 'benchmark-workloads', '-o', 'json'], check=False)
        if p.returncode == 0 and p.stdout.strip():
            d = json.loads(p.stdout)
            snap = d.get('status', {}).get('goldenSnapshotStatus', {}).get('goldenTag', {}).get('name', '')
            err = d.get('status', {}).get('goldenSnapshotStatus', {}).get('errorMessage', '')
            if snap:
                print(f'glutton golden snapshot ready: {snap}', flush=True)
                return
            if err:
                raise RuntimeError(f'glutton template failed: {err}')
        time.sleep(5)
    raise TimeoutError('Timed out waiting for glutton golden snapshot')

def set_freeze_mode(enabled: bool, recreate_tmpl: bool = False):
    val = 'true' if enabled else 'false'
    print(f'=== Setting ATE_GVISOR_FREEZE={val} on daemonset/atelet ===', flush=True)
    sh(['kubectl', 'set', 'env', 'daemonset/atelet', '-n', 'ate-system', f'ATE_GVISOR_FREEZE={val}'])
    sh(['kubectl', 'rollout', 'status', 'daemonset/atelet', '-n', 'ate-system', '--timeout=120s'])
    sh(['kubectl', 'rollout', 'restart', 'deployment/benchmark-worker', '-n', 'benchmark-workloads'])
    sh(['kubectl', 'rollout', 'status', 'deployment/benchmark-worker', '-n', 'benchmark-workloads', '--timeout=120s'])
    if recreate_tmpl:
        recreate_glutton_template()

def switch_to_microvm():
    print('=== Verifying WorkerPool/benchmark-worker is running microvm on microvm-pool ===', flush=True)
    cleanup_actors()
    sh(['kubectl', 'rollout', 'status', 'deployment/benchmark-worker', '-n', 'benchmark-workloads', '--timeout=180s'])
    recreate_glutton_template()

def query_prom_range(query: str, start_ts: int, end_ts: int) -> list[float]:
    params = urllib.parse.urlencode({'query': query, 'start': str(start_ts), 'end': str(end_ts), 'step': '5s'})
    url = f'http://localhost:9090/api/v1/query_range?{params}'
    p = sh(['kubectl', 'exec', '-n', 'benchmarking', 'deploy/prometheus', '--', 'wget', '-qO-', url], check=False)
    if p.returncode != 0 or not p.stdout:
        return []
    try:
        data = json.loads(p.stdout)
        vals = []
        for s in data.get('data', {}).get('result', []):
            for pt in s.get('values', []):
                vals.append(float(pt[1]))
        return vals
    except Exception:
        return []

def stats_of(vals: list[float]) -> dict:
    if not vals:
        return {'avg': None, 'p50': None, 'p95': None, 'max': None}
    s = sorted(vals)
    n = len(s)
    return {
        'avg': round(sum(s) / n, 4),
        'p50': round(s[int(n * 0.50)], 4),
        'p95': round(s[min(int(n * 0.95), n - 1)], 4),
        'max': round(s[-1], 4),
    }

def parse_locust_log(logs: str) -> dict:
    ops = {}
    for line in logs.splitlines():
        if '[locust]' not in line:
            continue
        clean = line.split('[locust]', 1)[1].strip()
        m1 = re.match(r'^(grpc|http|custom)\s+(\S+)\s+(\d+)\s+(\d+)\([\d.]+%\)\s+\|\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+\|\s+([\d.]+)\s+([\d.]+)', clean)
        if m1:
            op = m1.group(2)
            ops.setdefault(op, {})
            ops[op].update({
                'reqs': int(m1.group(3)),
                'fails': int(m1.group(4)),
                'avg_ms': int(m1.group(5)),
                'min_ms': int(m1.group(6)),
                'max_ms': int(m1.group(7)),
                'med_ms': int(m1.group(8)),
                'rps': float(m1.group(9)),
            })
        m2 = re.match(r'^(grpc|http|custom)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)$', clean)
        if m2:
            op = m2.group(2)
            ops.setdefault(op, {})
            ops[op].update({
                'p50_ms': int(m2.group(3)),
                'p90_ms': int(m2.group(7)),
                'p95_ms': int(m2.group(8)),
                'p99_ms': int(m2.group(10)),
                'p100_ms': int(m2.group(13)),
            })
    return ops

def run_single(name: str, users: int, duration_s: int, flags: list[str], tag: str, pool: str) -> dict:
    cleanup_actors()
    sh(['kubectl', 'delete', 'job', '--all', '-n', 'benchmarking', '--ignore-not-found'])
    job_name = f"runner-{name.replace('_', '-')}-{tag.replace('_', '-')}"[:63].rstrip('-')
    subs = {
        'JOB_NAME': job_name,
        'IMAGE': IMAGE,
        'TEST_FILE': '/app/tests/glutton.py',
        'DURATION': f'{duration_s}s',
        'USERS': str(users),
        'TAG': tag,
        'NAME': f'{name}_{tag}',
        'DEST': DEST,
        'RUNNER_CPU': '2000m',
        'RUNNER_MEMORY': '2Gi',
    }
    text = TMPL.read_text().replace('IfNotPresent', 'Always')
    for k, v in subs.items():
        text = text.replace('${' + k + '}', str(v))
    docs = list(yaml.safe_load_all(text))
    for doc in docs:
        if doc and doc.get('kind') == 'Job':
            doc['spec']['template']['spec']['containers'][0]['args'].extend(flags)
    manifest = yaml.safe_dump_all(docs)
    print(f'=== Submitting Job {job_name} (users={users}, duration={duration_s}s, pool={pool}) ===', flush=True)
    t0 = int(time.time())
    sh(['kubectl', 'apply', '-f', '-'], input_text=manifest)
    while time.time() - t0 < 600:
        r = sh(['kubectl', 'get', 'job', job_name, '-n', 'benchmarking', '-o', 'json'], check=False)
        if r.returncode == 0:
            st = json.loads(r.stdout).get('status', {})
            if st.get('succeeded', 0) >= 1 or st.get('failed', 0) >= 1:
                break
        time.sleep(5)
    t1 = int(time.time())
    logs = sh(['kubectl', 'logs', f'job/{job_name}', '-n', 'benchmarking', '--tail=400'], check=False).stdout
    Path(f'/tmp/result_{name}_{tag}.log').write_text(logs)
    sh(['kubectl', 'delete', 'job', job_name, '-n', 'benchmarking', '--ignore-not-found'], check=False)
    cleanup_actors()

    s_start = t0 + 12
    s_end = min(t1, t0 + duration_s + 5)
    np_filter = f'cloud_google_com_gke_nodepool="{pool}"'
    rec = {
        'tag': tag,
        'pool': pool,
        'users': users,
        'duration_s': duration_s,
        'window': [s_start, s_end],
        'client_ops': parse_locust_log(logs),
        'node_cpu_cores': stats_of(query_prom_range(f'sum(rate(container_cpu_usage_seconds_total{{container="node",{np_filter}}}[30s]))', s_start, s_end)),
        'worker_cpu_cores': stats_of(query_prom_range(f'sum(rate(container_cpu_usage_seconds_total{{namespace="benchmark-workloads",container="worker",{np_filter}}}[30s]))', s_start, s_end)),
        'atelet_cpu_cores': stats_of(query_prom_range(f'sum(rate(container_cpu_usage_seconds_total{{namespace="ate-system",container="atelet",{np_filter}}}[30s]))', s_start, s_end)),
        'node_mem_ws_mib': stats_of([v / (1024*1024) for v in query_prom_range(f'sum(container_memory_working_set_bytes{{container="node",{np_filter}}})', s_start, s_end)]),
        'worker_mem_ws_mib': stats_of([v / (1024*1024) for v in query_prom_range(f'sum(container_memory_working_set_bytes{{namespace="benchmark-workloads",container="worker",{np_filter}}})', s_start, s_end)]),
        'worker_mem_rss_mib': stats_of([v / (1024*1024) for v in query_prom_range(f'sum(container_memory_rss{{namespace="benchmark-workloads",container="worker",{np_filter}}})', s_start, s_end)]),
        'atelet_mem_ws_mib': stats_of([v / (1024*1024) for v in query_prom_range(f'sum(container_memory_working_set_bytes{{namespace="ate-system",container="atelet",{np_filter}}})', s_start, s_end)]),
        'node_disk_write_mbs': stats_of([v / (1024*1024) for v in query_prom_range(f'sum(rate(container_fs_writes_bytes_total{{container="node",{np_filter}}}[30s]))', s_start, s_end)]),
        'node_cpu_psi_pct': stats_of([v * 100.0 for v in query_prom_range(f'avg(rate(container_pressure_cpu_waiting_seconds_total{{container="node",{np_filter}}}[30s]))', s_start, s_end)]),
        'node_mem_psi_pct': stats_of([v * 100.0 for v in query_prom_range(f'avg(rate(container_pressure_memory_waiting_seconds_total{{container="node",{np_filter}}}[30s]))', s_start, s_end)]),
        'node_io_psi_pct': stats_of([v * 100.0 for v in query_prom_range(f'avg(rate(container_pressure_io_waiting_seconds_total{{container="node",{np_filter}}}[30s]))', s_start, s_end)]),
    }
    return rec

def main():
    all_records = {}
    if OUT_JSON.exists():
        try:
            all_records = json.loads(OUT_JSON.read_text())
        except Exception:
            all_records = {}

    modes = sys.argv[1:] if len(sys.argv) > 1 else ['gvisor_baseline', 'gvisor_frozen', 'microvm']
    first_gvisor = True
    for mode in modes:
        if mode == 'gvisor_baseline':
            set_freeze_mode(False, recreate_tmpl=first_gvisor)
            first_gvisor = False
            pool = 'gvisor-pool'
        elif mode == 'gvisor_frozen':
            set_freeze_mode(True, recreate_tmpl=first_gvisor)
            first_gvisor = False
            pool = 'gvisor-pool'
        elif mode == 'microvm':
            switch_to_microvm()
            pool = 'microvm-pool'
        else:
            raise ValueError(f'Unknown mode: {mode}')

        for name, users, duration_s, flags in TESTS:
            key = f'{name}_{mode}'
            print(f'\n================ RUNNING {key} on {pool} ================', flush=True)
            rec = run_single(name, users, duration_s, flags, mode, pool)
            all_records[key] = rec
            OUT_JSON.write_text(json.dumps(all_records, indent=2))
            sh(['gcloud', 'storage', 'cp', str(OUT_JSON), f'{DEST}/high_scale_telemetry.json'], check=False)
            print(f"[Saved {key}] client_ops={json.dumps(rec['client_ops'])} | cpu={rec['node_cpu_cores']} | worker_cpu={rec['worker_cpu_cores']} | mem={rec['node_mem_ws_mib']}", flush=True)

if __name__ == '__main__':
    main()
