# TODO: more hypervisors

Plans to onboard further hypervisors. Each becomes a site agent, driven by the
Sitebound Backup manager like the agents that exist.

## Planned agents: XCP-ng, libvirt/KVM, KubeVirt (added 2026-10-07)

Three more hypervisors, each as a **site agent** like ACME-DESK and ACME-PVE: it lives
beside the platform, writes to its own site's export, declares the job kinds it
can run, and turns every disk into the same stored run (raw sparse image, extent
map, per-file digests, `combined_digest`), so verify, the sweep, retention, the
repository page and file restore on the site's restore workstation work as they are.
Each inherits the house rules: restore always makes a **new** VM, network
disconnected and powered off unless asked, never powered on *and* connected; a
boot check judges by what the guest says. The sketches below are from the
platforms' documented interfaces and **must be checked against a real install
before building** — each needs a lab host first.

| # | Agent | Capture | Restore and boot check | Rough cost |
|---|---|---|---|---|
| A1 | **libvirt / KVM** (plain hosts: Debian/RHEL with libvirt, no Proxmox) — *first*: closest to code that exists | `virsh backup-begin` in **pull mode** exports a consistent point-in-time view of each disk over NBD (the same read path as the vSphere agent's NBD), stored raw sparse; **checkpoints** (QEMU dirty bitmaps) give true incrementals. Freeze through the QEMU guest agent when present. The domain XML kept per run, like `vm-config/`. Agent on the host (or `qemu+ssh://` to it), a key that can run only `virsh` | New disk files from the images, domain defined from the saved XML with a new name, UUID and MACs and every `<interface>` set `<link state='down'/>`; boot check = start it and ask the guest agent (`guest-ping`, as the Proxmox agent does) | ~1 week |
| A2 | **XCP-ng** (XAPI; also Citrix Hypervisor) | Through **XAPI** on the pool master, certificate pinned: snapshot the VM, export each VDI raw (`/export_raw_vdi?format=raw`, or NBD via `VDI.get_nbd_info`); **CBT** (`VDI.enable_cbt`, `VDI.list_changed_blocks`) gives incrementals, then the snapshot's data is dropped and only its CBT metadata kept. The VM's record (`VM.get_record`, `export_metadata`) kept per run. Credentials: XCP-ng's roles need an AD-backed pool, otherwise it is `root` — decide before building | Import each image into a new VDI on a scratch SR (`/import_raw_vdi`), create the VM from the saved record under a new name **with no VIFs** (added later only on request); boot check = start it and read `VM_guest_metrics` (guest tools / PV drivers report the OS and network), inconclusive without tools | ~2 weeks |
| A3 | **KubeVirt** (VMs on Kubernetes) — *possibly*: the most new ground | Inside the cluster with a ServiceAccount whose RBAC is limited to the VM namespaces and the export/snapshot kinds: `VirtualMachineSnapshot` (CSI volume snapshots, guest frozen through the agent), then a `VirtualMachineExport` serves each disk over HTTPS to the agent, stored raw sparse; the VM and DataVolume manifests kept per run. Incrementals: recent KubeVirt releases add changed-block tracking — check its maturity; full-only at first | A new DataVolume per disk uploaded through CDI's upload proxy, a new `VirtualMachine` under a new name in a **restore namespace** with a deny-all `NetworkPolicy` and `runStrategy: Halted`; boot check = start it there and wait for the `AgentConnected` condition, then delete the namespace | ~3 weeks |

**Order:** A1, then A2, then A3 only if a site needs it. Each one ends the same
way the Proxmox agent did: a capture, a verify, a sweep, a boot check that
passes, and a restore into a new VM, all from that site's own storage.
