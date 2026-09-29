# microVM spike results (2026-09-29)

Ticket: mvm-spike. Question: can we boot a microVM on this box (as kevin, minimal sudo) and run Docker inside
it? Answer: **yes, with Firecracker.** Artifacts in run/microvm/ (gitignored).

## Host
- kevin can open /dev/kvm rw (ACL `user:kevin:rw-`), so VMs RUN as kevin with no sudo. Ryzen 9 7950X (32t),
  122 GB RAM. Only tap networking needs a one-time sudo (guest internet); KVM/boot/disk do not.

## Firecracker v1.17.0 — the pick
- Boot-to-shell: **0.63 s**. VMM+touched-guest RSS: **~104 MB** (2 vCPU, 1 GB assigned).
- Kernel: CI vmlinux 6.1.128 (virtio-mmio); rootfs: CI ubuntu-22.04 ext4 (writable). Auto-login root on ttyS0,
  so the VM is drivable non-interactively over serial (feed stdin via a FIFO).
- **Docker inside: works.** dockerd 29.8.1 (static binaries on a second ext4 disk built with `mke2fs -d`, no
  sudo) started on the stock kernel; imported an image and `docker run --network none` executed a container
  offline. Used `--storage-driver vfs --iptables=false --bridge=none` to avoid depending on overlayfs/netfilter
  in the minimal CI kernel.

## Cloud Hypervisor v53.0 — not now
- Panics with the Firecracker CI kernel: CH uses virtio-**pci**, the CI kernel is virtio-**mmio** (`pci=off`),
  so it can't find the root disk. RSS ~67 MB before panic. Would need a PCI/distro kernel. Revisit only if we
  want CH's more mature virtiofs; Firecracker is the E2B choice and works out of the box.

## Caveats to fold into the guest-image ticket
- Minimal CI rootfs lacks common dirs (no /mnt). A real guest image must include the toolchain dirs.
- overlayfs + netfilter/veth/bridge were NOT exercised (vfs + no-net). Real use needs either a kernel built
  with overlay + netfilter + veth (so Docker uses overlay2 and container networking), or we stay on vfs + a
  host tap/NAT. Building a docker-capable guest kernel is the main remaining unknown.
- Container image pulls and container networking need the guest online → the one sudo step (kevin-owned tap +
  NAT), or a vsock/proxy path.

## Recommendation
Firecracker, direct-driven from ./agent (matches how bwrap is invoked; no containerd/Kata layer). Proceed to:
guest image (docker-capable kernel + baked rootfs), vsock model bridge, virtiofs worktree, then the
SANDBOX=microvm integration and the docker-in-VM smoke test.
