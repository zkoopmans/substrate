//go:build linux

// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"github.com/opencontainers/runtime-spec/specs-go"
	"go.ate.dev/substrate/internal/ocispec"
	"go.ate.dev/substrate/internal/proto/ateworkerpb"
	"go.ate.dev/substrate/internal/sizing"
)

type runsc struct {
	path     string
	actorUID string
	// actorDirs are the actor's directories, from the request.
	actorDirs *ateworkerpb.ActorDirs
	// size is the actor's declared limits.
	size sizing.SandboxSize
	// durableVolumes are the durable-dir volume names declared to the sandbox.
	durableVolumes []string
	// containers are the workload's containers, from the request.
	containers []*ateworkerpb.Container
	// flags holds the configured flags; must not be nil.
	flags *flagStore
}

// buildArgs constructs the runsc argument vector for command, appending
// containerName at the end if non-empty:
//
//	<combined globalFlags> -root <stateDir> <command> <subcommandFlags> <extraArgs> [<containerName>]
func (r *runsc) buildArgs(command, containerName string, extraArgs ...string) []string {
	cfg := r.flags.get()
	globalFlags := cfg.globalFlags(command)
	subcommandFlags := cfg.subcommandFlags(command)

	args := make([]string, 0,
		len(globalFlags)+2+1+len(subcommandFlags)+len(extraArgs)+1)
	args = append(args, globalFlags...)
	args = append(args, "-root", runscStateDir(r.actorDirs))
	args = append(args, command)
	args = append(args, subcommandFlags...)
	args = append(args, extraArgs...)
	if containerName != "" {
		args = append(args, containerName)
	}
	return args
}

// durableVolumeNames returns the sorted, deduplicated durable-dir volume names
// mounted by workload containers.
func durableVolumeNames(spec *ateworkerpb.WorkloadSpec) []string {
	var names []string
	for _, c := range spec.GetContainers() {
		for _, m := range c.GetDurableDirVolumeMounts() {
			names = append(names, m.GetName())
		}
	}
	slices.Sort(names)
	return slices.Compact(names)
}

// noRootContainerArgs returns the flag that tells runsc the sandbox has no
// root container. runsc refuses the flag for any other container.
func noRootContainerArgs(containerName string) []string {
	if containerName == ocispec.SandboxID {
		return []string{"-no-root-container"}
	}
	return nil
}

// writeSpec builds the container's OCI spec, shapes it for gVisor, and saves it
// into the container's bundle.
func (r *runsc) writeSpec(containerName string) error {
	bundlePath := ociBundlePath(r.actorDirs, containerName)
	var spec *specs.Spec
	if containerName == ocispec.SandboxID {
		// atelet makes only the application containers' bundles.
		if err := os.MkdirAll(bundlePath, 0o700); err != nil {
			return fmt.Errorf("while creating bundle %q: %w", bundlePath, err)
		}
		spec = ocispec.BuildSandbox(r.actorUID)
	} else {
		i := slices.IndexFunc(r.containers, func(c *ateworkerpb.Container) bool { return c.GetName() == containerName })
		if i < 0 {
			return fmt.Errorf("container %q is not in the workload spec", containerName)
		}
		spec = ocispec.Build(ocispec.Options{ActorUID: r.actorUID, ActorDirs: r.actorDirs, Container: r.containers[i]})
	}
	ocispec.ShapeGVisor(spec, ocispec.GVisorOptions{
		ActorUID:       r.actorUID,
		ActorDirs:      r.actorDirs,
		ContainerName:  containerName,
		DurableVolumes: r.durableVolumes,
		Size:           r.size,
	})
	return ocispec.Save(bundlePath, spec)
}

// createArgs builds the argv for `runsc create <container>`. Factored out so
// the argument construction can be unit-tested without executing runsc.
func (r *runsc) createArgs(containerName string, additionalArgs []string) []string {
	args := append(noRootContainerArgs(containerName),
		"-bundle", ociBundlePath(r.actorDirs, containerName),
		"-pid-file", pidFilePath(r.actorDirs, containerName),
	)
	args = append(args, additionalArgs...)
	return r.buildArgs("create", containerName, args...)
}

func (r *runsc) cmdCreate(ctx context.Context, out io.Writer, containerName string, additionalArgs []string) error {
	slog.InfoContext(ctx, "About to run runsc create", slog.String("container", containerName))

	if err := r.writeSpec(containerName); err != nil {
		return fmt.Errorf("while writing the OCI spec for %q: %w", containerName, err)
	}

	cmd := exec.CommandContext(ctx, r.path, r.createArgs(containerName, additionalArgs)...)
	cmd.Stdout = out
	cmd.Stderr = out

	err := cmd.Run()
	if err != nil {
		return fmt.Errorf("while running `runsc create`: %w", err)
	}

	return nil
}

func (r *runsc) cmdStart(ctx context.Context, out io.Writer, containerName string) error {
	slog.InfoContext(ctx, "About to run runsc start", slog.String("container", containerName))

	cmd := exec.CommandContext(ctx, r.path, r.buildArgs("start", containerName)...)
	cmd.Stdout = out
	cmd.Stderr = out

	err := cmd.Run()
	if err != nil {
		return fmt.Errorf("while running `runsc start`: %w", err)
	}

	return nil
}

// checkpointArgs builds the argv for `runsc checkpoint <container>`. Factored
// out so the argument construction can be unit-tested without executing runsc.
func (r *runsc) checkpointArgs(containerName, checkpointPath string, fsCheckpointPaths []string) []string {
	extra := make([]string, 0, 2+2*len(fsCheckpointPaths))
	extra = append(extra, "-image-path", checkpointPath)
	for _, p := range fsCheckpointPaths {
		extra = append(extra, "-fs-checkpoint-paths", p)
	}
	return r.buildArgs("checkpoint", containerName, extra...)
}

func (r *runsc) cmdCheckpoint(ctx context.Context, containerName, checkpointPath string, fsCheckpointPaths []string) error {
	slog.InfoContext(ctx, "About to run runsc checkpoint", slog.String("container", containerName))

	cmd := exec.CommandContext(ctx, r.path, r.checkpointArgs(containerName, checkpointPath, fsCheckpointPaths)...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	err := cmd.Run()
	if err != nil {
		return fmt.Errorf("while running `runsc checkpoint`: %w", err)
	}
	return nil
}

// fsCheckpointArgs builds the argv for `runsc fscheckpoint <container>`.
// Factored out so the argument construction can be unit-tested without
// executing runsc.
func (r *runsc) fsCheckpointArgs(containerName, checkpointPath string, fsCheckpointPaths []string) []string {
	extra := make([]string, 0, 2+2*len(fsCheckpointPaths))
	extra = append(extra, "-image-path", checkpointPath)
	for _, ddv := range fsCheckpointPaths {
		extra = append(extra, "-path", ddv)
	}
	return r.buildArgs("fscheckpoint", containerName, extra...)
}

func (r *runsc) cmdFsCheckpoint(ctx context.Context, containerName, checkpointPath string, fsCheckpointPaths []string) error {
	slog.InfoContext(ctx, "About to run runsc fscheckpoint", slog.String("container", containerName))

	cmd := exec.CommandContext(ctx, r.path, r.fsCheckpointArgs(containerName, checkpointPath, fsCheckpointPaths)...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	err := cmd.Run()
	if err != nil {
		return fmt.Errorf("while running `runsc fscheckpoint`: %w", err)
	}
	return nil
}

// restoreArgs builds the argv for `runsc restore <container>`. Factored out so
// the argument construction can be unit-tested without executing runsc.
func (r *runsc) restoreArgs(containerName, checkpointPath string) []string {
	args := append(noRootContainerArgs(containerName),
		"-bundle", ociBundlePath(r.actorDirs, containerName),
		"-image-path", checkpointPath,
		"-pid-file", pidFilePath(r.actorDirs, containerName),
	)
	return r.buildArgs("restore", containerName, args...)
}

// We take a checkpoint only of the sandbox, but we need to call restore on each
// container, using the same checkpoint.
func (r *runsc) cmdRestore(ctx context.Context, out io.Writer, containerName, checkpointPath string) error {
	slog.InfoContext(ctx, "About to run runsc restore", slog.String("container", containerName))

	if err := r.writeSpec(containerName); err != nil {
		return fmt.Errorf("while writing the OCI spec for %q: %w", containerName, err)
	}

	cmd := exec.CommandContext(ctx, r.path, r.restoreArgs(containerName, checkpointPath)...)
	cmd.Stdout = out
	cmd.Stderr = out
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("while running `runsc restore`: %w", err)
	}
	return nil
}

func (r *runsc) cmdDelete(ctx context.Context, containerName string) error {
	cmd := exec.CommandContext(
		ctx,
		r.path,
		r.buildArgs("delete", containerName)...,
	)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	err := cmd.Run()
	if err != nil {
		return fmt.Errorf("while running `runsc delete`: %w", err)
	}
	return nil
}

// pauseArgs builds the argv for `runsc pause <container>`. Factored out so the
// argument construction can be unit-tested without executing runsc.
func (r *runsc) pauseArgs(containerName string) []string {
	return r.buildArgs("pause", containerName)
}

// controlSocketPath returns the path to the Sentry control socket in runsc-state.
func (r *runsc) controlSocketPath(containerName string) string {
	if r.actorDirs == nil {
		return ""
	}
	stateDir := runscStateDir(r.actorDirs)
	// gVisor names the control socket runsc-<id>.sock in the root directory.
	// For sandbox containers, the ID is either containerName (e.g. "_pause") or actorUID.
	candidates := []string{
		filepath.Join(stateDir, fmt.Sprintf("runsc-%s.sock", containerName)),
		filepath.Join(stateDir, fmt.Sprintf("runsc-%s.sock", r.actorUID)),
	}
	for _, cand := range candidates {
		if _, err := os.Stat(cand); err == nil {
			return cand
		}
	}
	return candidates[0]
}

type urpcCall struct {
	Method string `json:"method"`
	Arg    any    `json:"arg"`
}

type urpcResult struct {
	Success bool   `json:"success"`
	Err     string `json:"err"`
	Result  any    `json:"result"`
}

// callSandboxControl attempts to issue an RPC over the sandbox's Unix domain control
// socket, bypassing the ~11ms runsc CLI fork/exec overhead.
func (r *runsc) callSandboxControl(ctx context.Context, containerName, method string) error {
	sockPath := r.controlSocketPath(containerName)
	if sockPath == "" {
		return fmt.Errorf("no actor dirs configured")
	}

	var d net.Dialer
	conn, err := d.DialContext(ctx, "unix", sockPath)
	if err != nil {
		return err
	}
	defer conn.Close()

	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	} else {
		_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	}

	reqBytes, err := json.Marshal(urpcCall{Method: method, Arg: nil})
	if err != nil {
		return fmt.Errorf("marshalling urpc call: %w", err)
	}
	if _, err := conn.Write(reqBytes); err != nil {
		return fmt.Errorf("writing to control socket: %w", err)
	}

	var res urpcResult
	if err := json.NewDecoder(conn).Decode(&res); err != nil {
		return fmt.Errorf("decoding urpc response: %w", err)
	}
	if !res.Success {
		return fmt.Errorf("urpc method %s returned error: %s", method, res.Err)
	}
	return nil
}

func (r *runsc) cmdPause(ctx context.Context, containerName string) error {
	slog.InfoContext(ctx, "About to run runsc pause", slog.String("container", containerName))
	if err := r.callSandboxControl(ctx, containerName, "containerManager.Pause"); err == nil {
		slog.DebugContext(ctx, "Paused sandbox via direct control socket", slog.String("container", containerName))
		return nil
	} else {
		slog.DebugContext(ctx, "Direct control socket pause failed, falling back to CLI",
			slog.String("container", containerName), slog.Any("error", err))
	}

	cmd := exec.CommandContext(ctx, r.path, r.pauseArgs(containerName)...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("while running `runsc pause`: %w", err)
	}
	return nil
}

// resumeArgs builds the argv for `runsc resume <container>`. Factored out so
// the argument construction can be unit-tested without executing runsc.
func (r *runsc) resumeArgs(containerName string) []string {
	return r.buildArgs("resume", containerName)
}

func (r *runsc) cmdResume(ctx context.Context, containerName string) error {
	slog.InfoContext(ctx, "About to run runsc resume", slog.String("container", containerName))
	if err := r.callSandboxControl(ctx, containerName, "containerManager.Resume"); err == nil {
		slog.DebugContext(ctx, "Resumed sandbox via direct control socket", slog.String("container", containerName))
		return nil
	} else {
		slog.DebugContext(ctx, "Direct control socket resume failed, falling back to CLI",
			slog.String("container", containerName), slog.Any("error", err))
	}

	cmd := exec.CommandContext(ctx, r.path, r.resumeArgs(containerName)...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("while running `runsc resume`: %w", err)
	}
	return nil
}

func (r *runsc) cmdState(ctx context.Context, containerName string) error {
	cmd := exec.CommandContext(
		ctx,
		r.path,
		r.buildArgs("state", containerName)...,
	)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("while running `runsc state`: %w", err)
	}
	return nil
}

// cmdList returns the container IDs runsc has a record of.
func (r *runsc) cmdList(ctx context.Context) ([]string, error) {
	cmd := exec.CommandContext(
		ctx,
		r.path,
		r.buildArgs("list", "")...,
	)
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("while running `runsc list`: %w", err)
	}
	return strings.Fields(out.String()), nil
}

// cmdKill sends signal to the given container's process(es) inside the gVisor
// sandbox. Used during graceful shutdown to propagate SIGTERM to the actor.
func (r *runsc) cmdKill(ctx context.Context, containerName, signal string) error {
	slog.InfoContext(ctx, "About to run runsc kill", slog.String("container", containerName), slog.String("signal", signal))

	cmd := exec.CommandContext(ctx, r.path, r.buildArgs("kill", "", containerName, signal)...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("while running `runsc kill`: %w", err)
	}
	return nil
}

// runWait runs `runsc wait [extraArgs...] <container>` and blocks until the
// waited-on condition completes.
func (r *runsc) runWait(ctx context.Context, containerName string, extraArgs ...string) error {
	cmd := exec.CommandContext(ctx, r.path, r.buildArgs("wait", containerName, extraArgs...)...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("while running `runsc wait`: %w", err)
	}
	return nil
}

// cmdWait blocks until the given container's process exits. Used during
// graceful shutdown to confirm the actor has stopped before ateworker exits.
func (r *runsc) cmdWait(ctx context.Context, containerName string) error {
	slog.InfoContext(ctx, "About to run runsc wait", slog.String("container", containerName))
	return r.runWait(ctx, containerName)
}

// cmdWaitRestore blocks until background page loading from `runsc restore -background` completes.
func (r *runsc) cmdWaitRestore(ctx context.Context, containerName string) error {
	return r.runWait(ctx, containerName, "-restore")
}

// cmdWaitFSRestore blocks until background filesystem page loading from
// `--fs-restore-image-path` completes for the given container.
func (r *runsc) cmdWaitFSRestore(ctx context.Context, containerName string) error {
	return r.runWait(ctx, containerName, "-fsrestore")
}
