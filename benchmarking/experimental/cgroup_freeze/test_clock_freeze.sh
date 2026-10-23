#!/bin/bash
set -e
CGROUP=/sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/clock_sb
mkdir -p $CGROUP

/tmp/density_test/bin/runsc --rootless --network=none --root=/tmp/density_test/runsc_root run --detach --bundle=/tmp/density_test/test_bundle clk_test
PID=$(/tmp/density_test/bin/runsc --rootless --network=none --root=/tmp/density_test/runsc_root state clk_test | grep '"pid":' | head -1 | tr -dc '0-9')
echo $PID > $CGROUP/cgroup.procs

echo "--- Clock Sample 1 (Initial) ---"
/tmp/density_test/bin/runsc --rootless --network=none --root=/tmp/density_test/runsc_root exec clk_test /bin/print_clock

echo "Pausing sandbox (Approach A)..."
/tmp/density_test/bin/runsc --rootless --network=none --root=/tmp/density_test/runsc_root pause clk_test

echo "Freezing cgroup..."
echo 1 > $CGROUP/cgroup.freeze

echo "Sleeping 3 seconds while frozen..."
sleep 3

echo "Thawing cgroup..."
echo 0 > $CGROUP/cgroup.freeze

echo "Resuming sandbox (Approach A)..."
/tmp/density_test/bin/runsc --rootless --network=none --root=/tmp/density_test/runsc_root resume clk_test

echo "--- Clock Sample 2 (Post-Thaw) ---"
/tmp/density_test/bin/runsc --rootless --network=none --root=/tmp/density_test/runsc_root exec clk_test /bin/print_clock

/tmp/density_test/bin/runsc --rootless --network=none --root=/tmp/density_test/runsc_root kill clk_test
sleep 0.5
/tmp/density_test/bin/runsc --rootless --network=none --root=/tmp/density_test/runsc_root delete clk_test
rmdir $CGROUP 2>/dev/null || true
echo "SUCCESS"
