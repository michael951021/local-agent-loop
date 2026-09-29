# Initiative: microVM sandbox (strong isolation + safe Docker)

> why: today agents run in a bwrap namespace sandbox locked to /work. That can't safely give them Docker —
> exposing the host daemon socket weakens host isolation (an agent could bind-mount the host FS). A microVM
> per worker gives each agent its own kernel and its own dockerd, so containers can't touch the host at all,
> and we gain isolation instead of losing it. Cost is ~2–3% wall time (the model, ~88% of wall time, runs on
> the host GPU and is untouched; the tax lands only on the ~13% tool/IO slice).

This is HARNESS work (the ./agent sandbox layer), not a contrib-loop task — the sandboxed agents cannot edit
the harness that jails them, and the spike needs sudo. Execute it directly on the loop repo, with the user for
the sudo/install steps. bwrap stays the default; microVM is a `SANDBOX=microvm` opt-in.

## Feasibility on this box (confirmed 2026-09-29)
- /dev/kvm present, AMD-V (svm), nested virt on. 16C/32T, 122 GB RAM. Fine for several VMs (~50–150 MB each).
- No GPU passthrough needed: agents reach the model server over the localhost proxy / bridge.py unix socket,
  never the GPU. This is the key simplification — the VMs are CPU+RAM+FS only.
- Kata / Firecracker / Cloud-Hypervisor are NOT in Ubuntu apt; install from upstream releases.
- E2B is this same idea (Firecracker microVMs) as a cloud product; its runtime is the `infra` repo the loop
  already targets. Self-hosting E2B is heavy (Nomad/Consul, multi-tenant); running Kata/Firecracker directly
  is far less machinery for a local 2-agent loop, and keeps everything on this box.

## Tickets

- [ ] **microVM spike: Kata vs Firecracker** — on this host, boot one guest that runs `docker run hello-world`
  inside it; measure boot time, memory, and a real build's I/O vs the current bwrap. Recommend a runtime with
  numbers in docs/microvm-eval.md. (id: mvm-spike) (after: -)
  why: pick the runtime from evidence before building the harness on it.
- [ ] **Guest image** — a scripted, reproducible rootfs + kernel with docker (or rootless podman) + git +
  Go/Node/Python/uv + rg/jq/make/gcc; build script under scripts/. (id: mvm-image) (after: mvm-spike)
  why: agents need their toolchain inside the VM, built reproducibly.
- [ ] **vsock model bridge** — extend bridge.py to tunnel the keepalive/model proxy over AF_VSOCK (host
  listener ↔ guest), so ANTHROPIC_BASE_URL inside the guest reaches the host model server with no guest
  networking. Test: guest curl to /v1 succeeds. (id: mvm-vsock) (after: mvm-spike)
  why: agents call the model over a socket today; the VM needs a host↔guest path with no network exposure.
- [ ] **virtiofs worktree** — mount each worker's git worktree into the guest (virtiofs, rw), plus the shared
  caches (.venv, repos, state/db) as .agent-shared does now; measure I/O. (id: mvm-fs) (after: mvm-image)
  why: the agent edits its worktree; the VM must see it with acceptable I/O.
- [ ] **agent runtime integration** — a SANDBOX=microvm path in ./agent that launches a per-worker VM instead
  of bwrap, wiring the worktree (virtiofs), model bridge (vsock), GH token and env; bwrap stays default.
  (id: mvm-run) (after: mvm-image, mvm-vsock, mvm-fs)
  why: make it a switch, so the loop runs either sandbox without a rewrite.
- [ ] **Docker-in-VM + smoke test** — prove an agent inside the VM runs `docker run` for a repo's
  container-based tests; add a smoke test; document that container tests now run under SANDBOX=microvm.
  (id: mvm-docker) (after: mvm-run)
  why: the whole point — container tests run fully isolated from the host.
