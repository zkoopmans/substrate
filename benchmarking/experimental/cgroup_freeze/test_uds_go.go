package main

import (
	"encoding/json"
	"fmt"
	"net"
	"os/exec"
	"time"
)

type urpcCall struct {
	Method string `json:"method"`
	Arg    any    `json:"arg"`
}

type urpcResult struct {
	Success bool   `json:"success"`
	Err     string `json:"err"`
	Result  any    `json:"result"`
}

func callURPC(sockPath, method string) (time.Duration, error) {
	conn, err := net.Dial("unix", sockPath)
	if err != nil {
		return 0, err
	}
	defer conn.Close()

	t0 := time.Now()
	reqBytes, err := json.Marshal(urpcCall{Method: method, Arg: nil})
	if err != nil {
		return 0, err
	}
	if _, err := conn.Write(reqBytes); err != nil {
		return 0, err
	}

	var res urpcResult
	if err := json.NewDecoder(conn).Decode(&res); err != nil {
		return 0, err
	}
	lat := time.Since(t0)
	if !res.Success {
		return lat, fmt.Errorf("urpc error: %s", res.Err)
	}
	return lat, nil
}

func main() {
	runscBin := "/tmp/density_test/bin/runsc"
	runscRoot := "/tmp/density_test/runsc_root"
	bundleDir := "/tmp/density_test/test_bundle"
	name := "go_uds"

	_ = exec.Command(runscBin, "--rootless", "--network=none", "-root="+runscRoot, "kill", name).Run()
	_ = exec.Command(runscBin, "--rootless", "--network=none", "-root="+runscRoot, "delete", name).Run()

	cmd := exec.Command(runscBin, "--rootless", "--network=none", "-root="+runscRoot, "run", "--detach", "-bundle="+bundleDir, name)
	if err := cmd.Run(); err != nil {
		panic(err)
	}
	defer func() {
		_ = exec.Command(runscBin, "--rootless", "--network=none", "-root="+runscRoot, "kill", name).Run()
		_ = exec.Command(runscBin, "--rootless", "--network=none", "-root="+runscRoot, "delete", name).Run()
	}()

	sockPath := fmt.Sprintf("%s/runsc-%s.sock", runscRoot, name)

	// Direct UDS Pause
	pLat, err := callURPC(sockPath, "containerManager.Pause")
	if err != nil {
		panic(err)
	}
	fmt.Printf("Go Direct UDS Pause Latency:  %v\n", pLat)

	// Direct UDS Resume
	rLat, err := callURPC(sockPath, "containerManager.Resume")
	if err != nil {
		panic(err)
	}
	fmt.Printf("Go Direct UDS Resume Latency: %v\n", rLat)
}
