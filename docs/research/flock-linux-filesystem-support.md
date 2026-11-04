flock(2) cross-process mutual exclusion across Linux filesystem types
Research note, compiled 2026-08-19. Primary sources cited inline as URLs.

SCOPE AND BASELINE BEHAVIOR

flock(2) (LOCK_EX/LOCK_SH/LOCK_NB) is an advisory lock. It is enforced only
against other cooperating callers of flock(); a process with suitable
permissions can ignore it and read/write the file directly. This is stated
directly in the man page: "flock() places advisory locks only; given
suitable permissions on a file, a process is free to ignore the use of
flock() and perform I/O on the file."
https://man7.org/linux/man-pages/man2/flock.2.html

On a plain local filesystem, flock() is implemented entirely inside the
kernel's VFS layer (fs/locks.c) against the in-memory inode. No filesystem-
specific code is required for correctness, which is why ordinary local
filesystems behave uniformly. This is a different mechanism from any
network-protocol lock: for local files there is nothing to "not reach" — the
lock lives in kernel memory attached to the inode, visible to every open
file description that resolves to that same inode, regardless of the path
used to reach it. For network filesystems (NFS, CIFS/SMB, some FUSE
filesystems), flock() additionally has to be translated into a real
wire-protocol lock request sent to a server; whether that translation
happens, and whether it is enforced, is exactly where the silent-failure
cases below come from.

ext4, xfs, btrfs — local, no network layer

Classification: (a) safe/guaranteed, for a single kernel instance.

These are ordinary local filesystems. flock() is handled generically by the
VFS against the in-memory inode; none of the three needs or has
filesystem-specific flock code. Locks correctly exclude any other process on
the same host that opens the same file, through any path that resolves to
the same inode (including different bind-mount paths — see the bind-mount
section). This guarantee is scoped to a single running kernel: if the
underlying block device is shared by two separate machines (e.g., the same
iSCSI/SAN LUN mounted read-write by two hosts running ext4/xfs/btrfs
independently), flock() provides no cross-machine exclusion at all, and
worse, none of these are cluster filesystems, so concurrent mounts of the
same block device from two hosts risk on-disk corruption independent of any
locking question. That failure mode is a filesystem-design issue, not a
flock() semantics issue, but it means "the same ext4 volume, from two
machines" is unsafe well before locking enters the picture.

tmpfs

Classification: (a) safe/guaranteed, for a single kernel instance.

tmpfs is handled by the same generic VFS locking path as ext4/xfs/btrfs — it
is not a network filesystem and has no special-cased flock behavior. Same
scope caveat as above: correctness is bounded to one kernel; tmpfs content
cannot be shared across machines at all, so the cross-machine question does
not apply.

NFSv3 (and NFSv2), with rpc.lockd/NLM

Classification: (b) works, but is mount-option and daemon dependent; also
version-gated on the client kernel for flock() specifically.

NFSv3 has no locking of its own; RFC 1813 leaves locking out of the base
protocol, and Linux implements it via the separate NLM (Network Lock
Manager) sideband RPC protocol, with rpc.statd implementing NSM (Network
Status Monitor) for reboot notification. The nfs(5) man page: "Selects
whether to use the NLM sideband protocol to lock files on the server. If
neither option is specified (or if lock is specified), NLM locking is used
for this mount point." nolock disables this: "When using the nolock option,
applications can lock files, but such locks provide exclusion only against
other applications running on the same client. Remote applications are not
affected by these locks." That is a documented, explicit silent-no-op mode
for cross-client exclusion — not a bug, but an easy misconfiguration trap,
and the man page notes it is actually required in some cases (mounting
/var, or against servers with no NLM support).
https://man7.org/linux/man-pages/man5/nfs.5.html

Separately, local_lock (NFSv2/v3 only, added in Linux 2.6.37) lets an
administrator keep flock and/or POSIX (fcntl) locks purely local while still
using NLM for the other kind, or force both local: "Specifies whether to use
local locking for any or both of the flock and the POSIX locking
mechanisms. mechanism can be one of all, flock, posix, or none." local_lock
is overridden by an explicit nolock/lock setting if both are given.
https://man7.org/linux/man-pages/man5/nfs.5.html

flock vs fcntl handling: historically these were NOT emulated over NFS at
all. The flock(2) man page: "Up to Linux 2.6.11, flock() does not lock
files over NFS (i.e., the scope of locks was limited to the local system)."
Since Linux 2.6.12, "NFS clients support flock() locks by emulating them as
fcntl(2) byte-range locks on the entire file," and because of that shared
implementation, "fcntl(2) and flock() locks do interact with one another
over NFS" — a caller using flock() and another using fcntl() byte-range
locks on the same file over NFS can block each other, which is not true
locally (flock and fcntl locks are independent lock classes on local
filesystems). Also: to place an exclusive flock() over NFS, "the file must
be opened for writing" (an LOCK_EX on a read-only fd will fail over NFS
where it would succeed locally).
https://man7.org/linux/man-pages/man2/flock.2.html

Net effect for NFSv3: when NLM is actually in use (the default), flock()
gives real cross-client exclusion via the server. It silently degrades to
local-only exclusion under nolock, under local_lock=all/local_lock=flock, or
on pre-2.6.12 kernels — all three are configuration/version states, not
protocol limitations.

NFSv4.0

Classification: (b) real cross-client exclusion by default, but with a
lease/grace-period model whose failure modes can silently drop a lock out
from under a process during a network partition or server reboot.

Locking is integrated directly into the protocol; there is no NLM/NSM and,
correspondingly, no nolock/local_lock option — the nfs(5) man page places
both of those options under "Options for NFS versions 2 and 3 only," and the
protocol spec is explicit about the architecture change: "Unlike NFSv3,
which used a series of ancillary protocols (e.g., NLM, NSM), within all
minor versions of NFSv4 a single RPC protocol is used to make requests to
the server. Facilities that had been separate protocols, such as locking,
are now integrated within a single unified protocol." (RFC 7530 §1.4.5,
"Locking Facilities in NFSv4" / carried into §9, "File Locking and Share
Reservations": https://www.rfc-editor.org/rfc/rfc7530.html )

flock() over NFSv4 uses the same client-side emulation as NFSv3: it is
turned into a real fcntl-style byte-range lock spanning the whole file and
sent to the server via the protocol's native LOCK operation (RFC 7530 §9),
not silently dropped. Locking state is lease-based: "The state associated
with file locks is maintained at the server under a lease-based model. The
server defines a single lease period for all state held by an NFS client"
(RFC 7530 §1.4.5). If a client fails to renew its lease — most commonly
because of a network partition — "the server MAY release all locks, both
share reservations and byte-range locks, that are held by a client if the
client's lease has expired" (RFC 7530 §9.6.3, "Network Partitions and
Recovery"). Crucially, this release is not pushed to the client: the client
only discovers it reactively, the next time it tries to use the stateid, at
which point the server returns NFS4ERR_EXPIRED (RFC 7530 §9.6.3).
https://www.rfc-editor.org/rfc/rfc7530.html

On the Linux client, when a held lock is found to have been lost, the kernel
marks it (NFS_LOCK_LOST) and subsequent read/write on that file description
fails with EIO rather than silently succeeding — i.e. the application does
get an error, just not until its next I/O, and not the SIGLOST signal that
older NFS documentation describes (SIGLOST delivery is inconsistent/legacy;
current Linux behavior is EIO on the affected fd). A module parameter,
recover_locks, controls whether the client attempts automatic lock recovery
after reconnection at all.
https://linux-nfs.org/wiki/index.php/Client_sessions_Implementation_Issues,
https://blogs.oracle.com/linux/nfsv4-courteous-server

Server reboot follows the same shape via a grace period: after restart, the
server only allows clients to reclaim pre-existing locks for a window
(RFC 7530 §9.6.2, "Server Failure and Recovery"); a lock a client believed
it still held can be silently lost if that client cannot reach the server
and reclaim before some other client takes the file, or if the client
itself missed the reclaim window. The practical implication for "crash-safe
mutual exclusion": NFSv4 flock is real (not a local no-op) under normal
operation, but it is not crash-safe in the sense of guaranteeing a lock
holder is still exclusive after any server reboot or client-side network
partition — the protocol trades that guarantee for liveness (the server
must eventually be able to free abandoned locks), and the previous holder
finds out only by hitting an error on next use, not via any proactive
notification.

NFSv4.1 and later (NFSv4.2)

Classification: (b) same real-lock behavior as NFSv4.0, with protocol-level
robustness improvements around retransmission, but the same lease/grace-
period loss-without-notice model.

RFC 8881 (NFSv4.1, obsoleting RFC 5661) keeps locking integrated into the
single protocol: file locking and share reservations are specified in §9;
state management, lease renewal, and crash recovery are in §8 (§8.3 "Lease
Renewal", §8.4 "Crash Recovery"). The major structural change in 4.1 is
sessions, which give exactly-once semantics to retried requests (including
lock requests) — this closes an ambiguity class from 4.0 where a retried
LOCK request after a network hiccup could be misinterpreted, but it does not
change the fundamental lease/grace-period loss-on-partition-or-reboot model
described above for 4.0. flock() is still client-side-emulated as a
whole-file fcntl-style byte-range lock, same as 4.0/4.1's predecessor.
https://www.rfc-editor.org/rfc/rfc8881.html

There is no nolock/local_lock equivalent for NFSv4.x mounts — per the
nfs(5) man page's own section grouping, those options are v2/v3-only, so on
NFSv4 there is no supported way to make flock() silently local-only via a
mount option; you would have to disable locking on the server, which is not
a client-mount-time toggle.
https://man7.org/linux/man-pages/man5/nfs.5.html

CIFS/SMB (mount.cifs / cifs.ko)

Classification: (c) unsafe/silent no-op in its historical default form and
under nobrl; (b) real but mandatory-by-default lock in the common
supported case; (a)-like real advisory POSIX lock only under SMB3.1.1 POSIX
extensions with matching client and server support.

This is the filesystem type with the sharpest silent-failure history of
everything in this survey. flock(2) support was not wired into cifs.ko at
all until a dedicated patch: "The flock system call locks the whole file
rather than a byte range and so is currently emulated by various other file
systems by simply sending a byte range lock for the whole file" — this
added a cifs_flock handler across the cifs file_operations structures,
landing for the Linux 5.5 cycle (commit d0677992d2af3d65f1c1c21de3323d09d
4891537, Steve French).
https://git.zx2c4.com/linux-rng/commit/fs/cifs/cifsfs.c?id=d0677992d2af3d65f1c1c21de3323d09d4891537

Before that (any kernel up to 5.4), flock() on a CIFS mount was a pure local
VFS lock with no request ever sent to the server — it always succeeded and
never excluded any other client, silently, on every CIFS mount regardless
of mount options. This is exactly the "succeeds locally, does not actually
exclude other clients" failure mode the research question describes, and it
was the unconditional default behavior of CIFS on Linux for a long time.

Since 5.5, flock() is emulated the same way NFS does it (a whole-file
byte-range lock request sent to the server), but two mount options still
produce silent or altered behavior:

nobrl: "Do not send byte range lock requests to the server. This is
necessary for certain applications that break with cifs style mandatory
byte range locks (and most cifs servers do not yet support requesting
advisory byte range locks)." Under nobrl, flock() (and fcntl byte-range
locks) are once again purely local — an explicit, documented, silent no-op
for cross-client exclusion, same shape as NFS's nolock.
https://man7.org/linux/man-pages/man8/mount.cifs.8.html

forcemandatorylock: "Even if the server supports posix (advisory) byte
range locking, send only mandatory lock requests." This forces the older,
stronger Windows-style mandatory lock model (enforced by the server against
all I/O on the locked range, not just other lock attempts) even when the
weaker/POSIX-compatible advisory mode is available.
https://man7.org/linux/man-pages/man8/mount.cifs.8.html

By default, absent SMB3.1.1 POSIX extensions, the byte-range lock CIFS
sends is a classic SMB mandatory lock, not a POSIX advisory lock — this is
stronger than flock()'s documented advisory semantics (it will actually
block ordinary I/O from other processes on the locked range, not just other
lock attempts), which is a behavior mismatch worth knowing about but not a
silent-failure case in itself. Real advisory (POSIX) byte-range locking
over SMB requires SMB3.1.1 POSIX protocol extensions, which need both
client and server support: Linux client support landed at kernel 5.8 and
was substantially improved by 6.13; server-side support requires a
recent Samba or the in-kernel ksmbd server. Against a stock Windows SMB
server, or an older Samba without the extensions, you get the
mandatory-lock behavior above, not POSIX advisory semantics, regardless of
client kernel version.
https://sambaxp.org/fileadmin/user_upload/sambaxp2023-Slides/French_sXP23_SMB3.1.1_POSIX.pdf,
https://wiki.samba.org/index.php/SMB3-Linux

Oplocks/leases (SMB2+ "leases" are the renamed, extended successor to SMB1
"oplocks") govern client-side caching, not lock semantics directly, but
they interact with reconnection: SMB2/SMB3 durable and persistent handles
are designed to survive a short disconnect and be reconnected using a
SMB2_CREATE_DURABLE_HANDLE_RECONNECT(_V2) create context, with lease state
restored via the LeaseState/Epoch fields on reconnect. This means a brief
network blip does not necessarily drop the lock the way an NFSv4 lease
timeout does — but it is dialect- and server-dependent (durable/persistent
handles are optional server features, most fully supported by SMB3.x
servers, not guaranteed on every implementation), and there have been
concrete client/server bugs where a durable handle failed to reconnect if
the leased file had been written to.
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-smb2/3309c3d1-3daf-4448-9faa-81d2d6aa3315,
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-smb2/62ba68d0-8806-4aef-a229-eefb5827160f

FUSE-backed filesystems

Classification: (c) unsafe/silent no-op by default for any FUSE filesystem
that does not implement the flock callback; (b) real, but entirely
dependent on that specific FUSE filesystem's implementation, when it does.

FUSE support for BSD-style flock() semantics is opt-in per filesystem, via
the FUSE_CAP_FLOCK_LOCKS capability, gated on the filesystem implementing a
flock handler: "This feature is enabled by default when supported by the
kernel and if the filesystem implements a flock() handler." The other half
of the design is the important one for this question — when a FUSE
filesystem does NOT implement that handler, the kernel does not reject or
error the flock() call; it silently handles it entirely in-kernel: "flock(2)
calls will be handled by the FUSE kernel module internally," i.e. exactly
the same purely-local, no-request-ever-sent-to-userspace behavior as CIFS
pre-5.5 or NFS's nolock, but here it is the norm for any FUSE filesystem
that hasn't specifically opted in, not an explicit mount-option choice. The
related FUSE_CAP_POSIX_LOCKS flag ("indicates that the filesystem supports
'remote' locking") governs fcntl-style byte-range locks the same way and is
"enabled by default when supported by the kernel, and if getlk() and
setlk() handlers are implemented" — again opt-in per filesystem.
(FUSE_FLOCK_LOCKS/FUSE_RELEASE_FLOCK_UNLOCK were added to the kernel FUSE
ABI at protocol version 7.17.)
https://github.com/libfuse/libfuse (fuse_common.h capability flags),
https://patchwork.kernel.org/patch/9771225/

In practice this means flock() "working" on a FUSE mount tells you nothing
about whether it does anything useful — it depends entirely on which
userspace filesystem is mounted. sshfs is a concrete, documented example:
without explicit forwarding support, flock requests are handled locally by
the kernel and never reach the remote SFTP server, so two clients each
sshfs-mounting the same remote directory get zero cross-client exclusion
even though flock() returns success on both sides; a patch exists to
forward lock requests to servers that advertise an SFTP locking extension,
but falls back to "old behavior" (local-only) against servers that don't,
returning ENOSYS internally rather than surfacing a hard failure to the
caller.
https://github.com/libfuse/sshfs/pull/55
Other common FUSE filesystems representing remote/object storage (e.g.
s3fs-fuse) generally do not implement the flock handler at all, so the same
silent local-only behavior applies. Treat every FUSE mount as
guilty-until-proven-innocent for flock() correctness — check the specific
filesystem's documentation/source for a flock or setlk implementation
rather than assuming protocol-level support.

overlayfs

Classification: (b) correct for a same-host overlay with only local
upper/lower layers, on a kernel new enough to include the lock-inode fix
(mainline since the 4.9 era); (c)/unresolved for overlays combined with
NFS export or network-backed layers.

Early overlayfs associated file locks with the underlying (upper or lower)
inode rather than the overlay's own inode. This broke on copy-up: a lock
taken on a lower-layer file could be silently lost/invalidated the moment a
write triggered copy-up to the upper layer, since the write produces a new
upper inode distinct from the one the lock was attached to. This was fixed
by Miklos Szeredi's "locks: fix file locking on overlayfs" patch, which
introduced a locks_inode() helper so all lock/lease code paths use the
overlay's own inode instead of file_inode()'s underlying inode — "This
allows locks to be persistent across copy-up" — plus a superblock flag
(MS_NOREMOTELOCK) to stop the overlay from forwarding lock calls into the
underlying filesystem's own remote-lock operations. This landed in the
4.9-era mainline (and was backported into stable 4.4.16/4.4.17).
https://lkml.iu.edu/hypermail/linux/kernel/1609.2/00292.html,
https://cdn.kernel.org/pub/linux/kernel/v4.x/ChangeLog-4.4.16

On a reasonably current kernel with a purely local upper and lower
(ext4/xfs/btrfs, no network layer), flock() on the merged view behaves like
a normal local file — correct, same-inode-in-kernel-memory semantics as
above. The known gap is when overlayfs is layered with a network filesystem
involved, either as a lower/upper layer or via NFS-exporting the overlay
mount itself. A concrete, apparently still-unresolved report on the
linux-unionfs list: with an overlay mounted with nfs_export=on and accessed
over NFSv4, flock(LOCK_EX) on a file opened O_RDONLY fails with EBADF,
because NFSv4 requires a write-opened fd for an exclusive lock (see the
NFSv3/v4 section above) and overlayfs's read-only/read-write fd handling
interacts badly with that requirement; switching to NFSv3 as a workaround
surfaced a different bug (DT_UNKNOWN from readdir). No fix is recorded in
that thread.
https://marc.info/?l=linux-unionfs&m=152093050204357&w=2
Treat "overlayfs plus any network filesystem in the stack" as
config/version-dependent at best, and verify empirically rather than
assuming the local-overlay guarantee extends to it.

Bind mounts and mount namespaces / containers

Classification: (a) safe/guaranteed when it is genuinely the same
underlying kernel mount reached two ways; (b)/trap when it looks the same
but is actually two independent mounts.

flock() locks are not tied to a path or a mountpoint; they are tied to an
inode (for flock, effectively the open file description acting on that
inode) inside one running kernel. A bind mount does not create a new
filesystem instance — per the man page's NOTES on lock scope, and per the
general Linux mount model, a bind mount is a second view (vfsmount) of the
same already-mounted filesystem, resolving to the same inode. Two processes
that reach the same file via two different bind-mount paths — including
processes in two different mount namespaces that each have their own bind
mount of the same original mount — see the same inode and therefore
correctly exclude each other via flock(), with no special handling needed.
This also holds across containers that share a bind-mounted volume,
provided that volume is, under the hood, one single kernel mount that both
containers' mount namespaces point into (the standard way container
runtimes share a host directory into multiple containers).
https://man7.org/linux/man-pages/man2/flock.2.html,
https://utcc.utoronto.ca/~cks/space/blog/linux/BindMounts

The trap is when two containers (or two processes) each perform their own
independent mount of the same underlying network resource, rather than
sharing one host-side mount via a bind mount — e.g. each container runs its
own `mount -t nfs` or `mount -t cifs` of the same export/share, instead of
one host mount bind-mounted into both. Whether that still works correctly
depends on the filesystem, not on any inode-sharing guarantee:
for NFS, the Linux client shares its data/attribute cache across multiple
concurrent mounts of the same export by default (the sharecache mount
option, on by default): "If neither option is specified, or if the
sharecache option is specified, then a single cache is used for all mount
points that access the same export," and this is explicitly about the
*data and attribute* cache, not about locking — flock()/fcntl() locks are
always sent over the wire to the server's lock manager regardless of the
sharecache setting (barring nolock/local_lock), so NFS cross-mount
exclusion is correct either way as long as server-side locking is actually
enabled. The nosharecache alternative gives each mount its own cache and
warns that "multiple cached copies of the same file on the same client can
become out of sync following a local update of one of the copies" — a data
consistency footgun, separate from the locking question.
https://man7.org/linux/man-pages/man5/nfs.5.html
For CIFS or any FUSE filesystem, correctness of two independent same-host
mounts again comes down entirely to whether that filesystem's flock is
wired to the server/backend at all (see the CIFS and FUSE sections) — if
it is only locally emulated (nobrl, pre-5.5 CIFS, a FUSE fs without a flock
handler), then two independent mounts of the same remote resource on one
host will NOT exclude each other, silently, even though it is a single
machine, because each mount has its own local-only lock state with nothing
underneath tying them together.

The safe design rule for containers: share one host-side mount via bind
mounts/volumes into every container that needs coordinated locking, rather
than letting each container independently mount the same network share —
that removes the dependency on the specific filesystem's cross-mount
locking behavior and falls back to the always-safe same-inode guarantee.

Paths shared across separate physical machines via a network filesystem, in general

Classification: real (not silently local) cross-machine exclusion is
achievable, but "crash-safe" is not, for any of the network filesystems in
this survey.

Across genuinely separate machines there is no shared kernel state at all —
correctness depends 100% on the network filesystem's own wire protocol
enforcing the lock at the server, which means every silent-failure mode
above (NFS nolock/local_lock, CIFS nobrl or pre-5.5 kernels, an
unimplemented FUSE flock handler) is not merely a same-host inconvenience,
it is total loss of cross-machine exclusion with the syscall still
returning success. Even where locking is genuinely wired up end to end
(NFSv3 with NLM active, NFSv4.x, CIFS with byte-range locks enabled), none
of them provide a crash-safe guarantee in the distributed-systems sense:
NFS's lease/grace-period model can and does silently release a client's
lock during a server reboot or a network partition long enough to miss
lease renewal, with the original holder finding out only via an error
(NFS4ERR_EXPIRED at the server, surfacing as EIO to the Linux application)
on its next use of the file — not via any proactive notification at the
moment the lock is actually lost (RFC 7530 §9.6.2, §9.6.3). NLM/NFSv3 has
an analogous reboot-recovery window coordinated via rpc.statd/NSM, with the
same shape of risk. CIFS durable/persistent handles mitigate brief
disconnects for SMB2/3 but are optional server features and have had their
own reconnection bugs. In all cases, the general pattern to design around
is: a network filesystem's flock() can tell you "no other client currently
holds this lock," but it cannot promise "I will still hold this lock, or
be told the instant I stop holding it," across a server crash or a network
partition — any design that needs true crash-safe distributed mutual
exclusion should use a system built for that purpose (a consensus-backed
lock service) rather than relying on flock() over a network filesystem.
https://www.rfc-editor.org/rfc/rfc7530.html,
https://www.rfc-editor.org/rfc/rfc8881.html
