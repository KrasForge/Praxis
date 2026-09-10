# Execution isolation

`LocalProcessExecutor` uses LinuxIsolation by default. ShellExecutor and
CommandValidator inherit that policy. You must install Bubblewrap and permit it
to make user namespaces. If you do not, execution fails. There is no fallback to
native access on the host. CI installs Bubblewrap on disposable Ubuntu runners
and permits their user namespaces. Another operating system needs a dedicated
Linux worker, or an executor that you qualified independently.

The sandbox uses these controls:

- An empty root
- Separate user, PID, network, IPC and UTS namespaces
- Dropped capabilities
- A private `/proc`, `/dev` and `/tmp`
- A writable bind of the process workspace only

The read-only runtime mounts default to `/usr`, `/bin`, `/lib`, `/lib64` and the
base Python installation of the host. These are runtime images that Praxis
trusts explicitly. They are not mounts that Praxis derives from `argv`. A host
can supply a narrower `LinuxIsolation(runtime_roots=...)`. Never include private
data, and never include a parent of the workspace. A Python virtualenv launcher
resolves to the mounted base interpreter. An extra package or tool must come in
a runtime image that the host approved.

A subprocess receives only the declared environment, together with the provider
values that Praxis authorized. It inherits pipes, not a terminal, and the
launcher makes a new session. A symlink in a workspace cannot show a file that
is absent from the mount namespace. The validation at the boundaries of snapshot
and import rejects an unsafe link. The host must not replace the metadata of a
provider, or a mounted runtime root, while a process runs.

The Codex, Claude and DeepSeek adapters need `isolated_worker=True`. This flag
is an assertion by the host that something else confines the worker. The flag
does not make a sandbox. Mount only the current workspace of that worker,
together with the runtime and resources that you approved. Apply the network
policy and the credentials of the provider.

`isolation=None` on LocalProcessExecutor is for trusted code, or for a host that
something else isolates already. It does not advertise isolation. An untrusted
spec cannot set either option of the host.

A namespace limits visibility and network access. It does not apply a quota for
memory, disk, the count of PIDs or CPU. Use cgroups and filesystem quotas for
those limits. See TM-3 and TM-9, and the
[security model of Bubblewrap](https://github.com/containers/bubblewrap#sandbox-security).
An OS kernel that an attacker controls stays outside the trust boundary. So does
an installed Python adapter that an attacker controls.

Praxis bounds the native stdout and stderr, and the accumulation of the Codex
stream, to 1 MiB for each stream and attempt by default. If a process goes above
the limit, Praxis stops it and returns an explicit outcome that is not a
success. A host can configure `LocalProcessExecutor.max_output_bytes`.
