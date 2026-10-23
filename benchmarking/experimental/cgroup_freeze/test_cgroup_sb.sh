#!/bin/bash
set -e
mkdir -p /sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/test_sb
/tmp/density_test/bin/runsc --rootless --network=none --root=/tmp/density_test/runsc_root run --detach --bundle=/tmp/density_test/test_bundle sb1
PID=$(/tmp/density_test/bin/runsc --rootless --network=none --root=/tmp/density_test/runsc_root state sb1 | grep '"pid":' | head -1 | tr -dc '0-9')
echo "Sandbox PID is $PID"
echo $PID > /sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/test_sb/cgroup.procs
echo "cgroup.procs contains: $(cat /sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/test_sb/cgroup.procs)"
echo 1 > /sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/test_sb/cgroup.freeze
sleep 0.1
echo "events after freeze: $(cat /sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/test_sb/cgroup.events | tr '\n' ' ')"
echo 0 > /sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/test_sb/cgroup.freeze
sleep 0.1
echo "events after thaw: $(cat /sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/test_sb/cgroup.events | tr '\n' ' ')"
/tmp/density_test/bin/runsc --rootless --network=none --root=/tmp/density_test/runsc_root kill sb1
sleep 0.5
/tmp/density_test/bin/runsc --rootless --network=none --root=/tmp/density_test/runsc_root delete sb1
rmdir /sys/fs/cgroup/user.slice/user-612609.slice/user@612609.service/app.slice/test_sb
echo "FINISHED SUCCESSFULLY"
