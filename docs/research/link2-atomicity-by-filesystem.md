# link(2) as an atomic create-if-absent primitive, by filesystem and mount type

Research date 2026-08-19. Question: which Linux filesystems and mount
configurations let link(oldpath, newpath) be relied on as a distributed
idempotency check, where EEXIST is load-bearing — it must be impossible for
link() to silently overwrite an existing newpath, and it must be atomic
against a concurrent link() from another process or machine.

Classification key used throughout:
A = atomic and EEXIST-safe by specification.
B = atomic and safe only under a specific named configuration.
C = not safe, or not supported.

## Baseline: what the spec and the man page actually promise

POSIX link(): "shall atomically create a new link for the existing file and
the link count of the file shall be incremented by one." EEXIST is defined
as "The path2 argument resolves to an existing directory entry or refers to
a symbolic link." The POSIX rationale flags one race that is NOT covered by
the atomicity guarantee: "Any part of the path of a file could be changed in
parallel to a call to link(), resulting in unspecified behavior" — i.e. the
guarantee is about the final component, not the whole path; this motivated
linkat() with a directory file descriptor.
Source: https://pubs.opengroup.org/onlinepubs/9699919799/functions/link.html

man7 link(2): "If newpath exists, it will not be overwritten." Also
documents the same-filesystem restriction: "oldpath and newpath are not on
the same mounted filesystem. Linux permits a filesystem to be mounted at
multiple points, but link() does not work across different mounts, even if
the same filesystem is mounted on both." And the NFS caveat: "On NFS
filesystems, the return code may be wrong in case the NFS server performs
the link creation and dies before it can say so. Use stat(2) to find out if
the link got created."
Source: https://man7.org/linux/man-pages/man2/link.2.html

Linux kernel locking: for link(), the kernel documentation on directory
locking states the parent directory is locked exclusive before the target
name is looked up and before the link is created ("lock the parent
(exclusive)"), same as create(). This is the mechanism that makes local
link() atomic: the existence check and the link creation happen under one
held exclusive lock on the parent directory's inode, so no other create,
link, or rename targeting that same name can interleave.
Source: https://docs.kernel.org/6.9/filesystems/directory-locking.html

A 2007 LKML thread confirms the cross-mount check is by mount object
(vfsmount), not by underlying superblock: the kernel compares old_nd.mnt !=
nd.mnt and refuses to link across two different mount points even when
old_nd.mnt->mnt_sb == nd.mnt->mnt_sb (literally the same filesystem mounted
twice, or the same filesystem exposed through a bind mount). This was
proposed to be relaxed to a same-superblock check and rejected, because the
mount-based check is deliberately used as a security boundary: bind-mounting
a subtree confines what can be hard-linked out of it.
Source: https://lkml.iu.edu/hypermail/linux/kernel/0712.2/1169.html (thread,
see follow-ups for the maintainer rationale)

## Local ext4, xfs

Classification: A, atomic and EEXIST-safe by specification, no filesystem-
specific caveats beyond the generic VFS behavior above.

ext4 and xfs both implement the standard ->link inode operation and go
through the same do_linkat / vfs_link path described above; the parent
directory's exclusive lock covers the whole lookup-then-link sequence, so
two concurrent link() calls to the same newpath on the same local ext4 or
xfs filesystem cannot both succeed, and the loser reliably gets EEXIST. The
only ext4-specific limit is the hard-link-count ceiling (65,000 on ext4,
returned as EMLINK, not related to the EEXIST/atomicity question).
Source (limit): https://man7.org/linux/man-pages/man2/link.2.html

## Local btrfs

Classification: A within a single subvolume; C (ENOTSUP/EXDEV-class
failure) across subvolumes — this is a real caveat for the "distributed
idempotency" use case if oldpath and newpath are expected to be able to
live in different subvolumes.

Within one btrfs subvolume, link() goes through the same generic VFS
locking as ext4/xfs and is atomic and EEXIST-safe. But btrfs treats each
subvolume as if it were a separate mounted filesystem for this purpose:
btrfs_link checks whether the root of the destination directory equals the
root of the source file, and returns -EXDEV ("Invalid cross-device link")
if they differ, i.e. hard links cannot cross a subvolume boundary even
though both subvolumes are on the same btrfs filesystem and possibly the
same mount. This means a naive "call link() and check EEXIST" idempotency
scheme silently changes failure mode (EXDEV instead of success/EEXIST) if
someone reorganizes subvolumes under the two paths.
Sources: https://lwn.net/Articles/579009/ (Btrfs subvolumes and snapshots);
corroborating discussion at
https://itsfoss.gitlab.io/blog/getting-invalid-cross-device-link-doing-a-cp--l---same-volume/

## tmpfs

Classification: A, atomic and EEXIST-safe by specification.

tmpfs is a normal in-VFS filesystem (shmem) that implements hard links via
its own inode_operations (shmem_link, historically fs/libfs.c's
simple_link for the generic case) and is subject to the same generic VFS
directory locking as any other local filesystem — the parent directory's
exclusive lock covers the check-then-link sequence. Kernel changelog
history shows shmem_link is actively maintained (e.g. fixes to link-count
accounting), consistent with it being a first-class implementation of the
same VFS contract, not a degraded one.
Source: https://www.kernel.org/doc/html/latest/filesystems/tmpfs.html and
corroborating kernel patch discussion at
https://lkml.rescloud.iu.edu/1902.2/05480.html ("tmpfs: fix uninitialized
return value in shmem_link")

## NFSv3

Classification: B, atomic and EEXIST-safe only when treated as "trust the
server's synchronous EEXIST reply, but treat an RPC-level failure (timeout,
retransmit, server crash before reply) as ambiguous and resolve it with an
explicit stat() of the link-count, not by retrying blindly."

LINK is part of the NFSv3 protocol (procedure 3.3.15). NFSv3 does not do
cross-device links: NFS3ERR_XDEV is defined explicitly for "Attempt to do a
cross-device hard link," mirroring the local EXDEV same-filesystem
restriction — you cannot hard-link across two different NFS mounts/exports,
even against the same server, unless oldpath and newpath resolve to the
same exported filesystem.
Source: https://www.rfc-editor.org/rfc/rfc1813.html (section 3.3.15 LINK;
error list including NFS3ERR_XDEV)

Server-side, the LINK RPC is a single synchronous operation, so on the
server the check-for-existing-name-then-create-link step is atomic (same
requirement structure as the local case, enforced by the server's own
local filesystem). The load-bearing caveat is client-side and network-
level, not server-side atomicity: NFSv3 has no exactly-once RPC semantics.
If the client sends LINK and the connection drops or the server crashes
before the reply arrives, the client cannot distinguish "link was created"
from "link was never created" from the RPC return code alone. This is
exactly the ambiguity man7's link(2) page documents ("the return code may
be wrong in case the NFS server performs the link creation and dies before
it can say so. Use stat(2) to find out if the link got created") and it is
also why the canonical NFS-safe locking idiom explicitly does NOT trust a
single link() return value — it falls back to stat() and checks whether
the link count reached 2.
Sources: https://man7.org/linux/man-pages/man2/open.2.html (O_EXCL/NFS
section, which documents and recommends this exact link()+stat() idiom);
worked description at
https://www.experts-exchange.com/questions/10078625/atomic-locking-over-NFS-with-link-2-stat-2.html

Practical implication for your use case: NFSv3 LINK's EEXIST is trustworthy
as a "someone already has it" signal when it comes back as an explicit
reply. It is NOT safe to treat "I got a network error / timeout" as "link
did not get created" — you must verify by stat()'ing the destination
(checking link count / identity) before concluding failure, because a
duplicate un-acknowledged LINK request replayed by client RPC retry logic
combined with a slow server has produced real-world false negatives.
NFSv3 has no duplicate-request cache mandated by the RFC the way NFSv4.1
sessions do (see below), so retransmission-triggered double execution or
false-failure is a genuine, spec-acknowledged risk, not a theoretical one.

## NFSv4 (4.0) and NFSv4.1/4.2

Classification: B for NFSv4.0 (same crash-ambiguity caveat as v3, slightly
better because it's COMPOUND/session-agnostic but still no mandatory
duplicate-request cache); A-with-network-caveats for NFSv4.1+ when the
client/server actually negotiate sessions, because NFSv4.1 sessions were
specifically designed to close this gap.

LINK is part of the protocol (RFC 7530 section 16.9, formerly numbered
16.10 in some drafts). NFS4ERR_EXIST is returned when "the target name
already exists" in the destination directory, which requires the server to
check for a pre-existing name as part of servicing the single LINK
operation. Section 14.3 characterizes LINK as a synchronous modifying
operation — the server commits the change durably before replying — so the
check-then-create step is atomic on the server, same structural guarantee
as the local case.
Source: https://www.rfc-editor.org/rfc/rfc7530.html (section 16.9 LINK;
section 14.3 Synchronous Modifying Operations)

Cache-consistency caveat that is easy to miss: NFSv4's attribute and
directory caching is explicitly non-strict. Section 10.6 lets a client
cache directory/attribute state "for a duration determined by the client"
before re-querying the server, and this can make a client observe a stale
view of whether newpath exists — but note this affects things like stat()
or readdir() cache staleness, not the LINK RPC's own reply, which is a
direct synchronous round trip to the server. In other words: don't
substitute a cached lookup/getattr for a live LINK call when you need the
authoritative EEXIST signal; the caching caveat is about ancillary reads,
not about LINK's own result.
Source: https://www.rfc-editor.org/rfc/rfc7530.html (section 10.6, Attribute
Caching; section 10.3.1, Data/attribute caching discussion)

The crash-ambiguity problem that plagues NFSv3 (and NFSv4.0, which has no
sessions) is specifically what NFSv4.1 sessions were built to close.
Sessions give the server a per-client duplicate-request cache and
"exactly once" semantics for non-idempotent requests such as LINK: a
retransmitted LINK request that the server already executed is answered
from the reply cache with the original result, rather than re-executed or
left ambiguous. This means that on NFSv4.1 (or 4.2) with sessions actually
in effect, a network retry after a dropped reply no longer produces the
"client can't tell if it happened" ambiguity that man7 warns about for
plain NFS — the client eventually gets the true original answer instead of
a manufactured EEXIST or a false success.
Source: https://www.rfc-editor.org/rfc/rfc5661.html (NFSv4.1 sessions,
exactly-once semantics description); see also
https://datatracker.ietf.org/doc/html/rfc5661

Same-filesystem/export constraint: NFSv4 uses a single-server-filesystem-
namespace model, but hard links still cannot cross a filesystem boundary
inside that namespace (an fsid boundary), same restriction as v3's
NFS3ERR_XDEV — link across two different exports, or across a pseudo-fs
mount point where the server stitches multiple real filesystems into one
NFSv4 namespace, fails.

Practical implication: NFSv4.0 without sessions inherits the NFSv3-style
crash ambiguity — do the link()+stat() dance. NFSv4.1/4.2 with sessions
negotiated (the Linux client negotiates this automatically when both ends
support 4.1+) removes the main practical objection, but you are trusting
"the client and server actually got 4.1 sessions, not 4.0", which is a
mount/negotiation detail worth confirming (e.g. via nfsstat or mount
options) rather than assuming.

## CIFS/SMB (Linux cifs.ko client)

Classification: B, atomic and EEXIST-safe only when both (1) the remote
share's filesystem actually supports hard links (effectively NTFS/ReFS, not
FAT/exFAT), and (2) you are relying on the Linux cifs.ko client's own
link(2) implementation, not raw SMB from some other client stack.

Hard links are part of SMB2/SMB3 (no Unix extensions required): the
FileLinkInformation (SMB2 variant, FILE_LINK_INFORMATION_TYPE_2) SET_INFO
request creates a hard link server-side. Critically, the wire structure has
an explicit ReplaceIfExists boolean: "Set to TRUE to indicate that if the
link already exists, it SHOULD be replaced with the new link. Set to FALSE
to indicate that the link creation operation MUST fail if the link already
exists," and MS-FSA specifies the server "MUST be failed with
STATUS_OBJECT_NAME_COLLISION" when the target name already exists and
ReplaceIfExists is FALSE.
Sources:
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fscc/58f44021-120d-4662-bf2c-9905ed4940dc
(FileLinkInformation for the SMB2 Protocol);
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fscc/280df540-49d6-4a06-b337-3cdef045cb2a
(FileLinkInformation for the SMB Protocol)

This makes create-if-absent semantics a matter of which value the client
sets, not something SMB gives you for free. Checked directly against
current upstream Linux kernel source
(torvalds/linux, fs/smb/client/smb2inode.c, function smb2_create_hardlink /
the SMB2_OP_HARDLINK case inside smb2_compound_op): the client hardcodes
vars->link_info.ReplaceIfExists = 0; it is not exposed as a parameter, so
Linux's cifs.ko always requests fail-if-exists semantics when your program
calls link(2) on a cifs mount — matching POSIX link() semantics rather
than "replace." The actual EEXIST-vs-atomic check then happens server-side
(same structural guarantee as local filesystems: one SET_INFO request,
server does exist-check-then-create as a unit), so correctness at that
point depends on the SMB server's own implementation being correct, which
for Windows/Samba against NTFS-class backing stores is expected to hold.
Source: https://raw.githubusercontent.com/torvalds/linux/master/fs/smb/client/smb2inode.c
(current upstream source, function smb2_create_hardlink and the
SMB2_OP_HARDLINK branch of smb2_compound_op)

Backing-filesystem caveat: NTFS supports hard links; FAT and exFAT do not.
If the SMB server is re-exporting a FAT/exFAT volume (e.g. some NAS
devices, or a share backed by a USB stick formatted FAT32), the hard-link
request will fail outright — not silently succeed, but it is not usable at
all rather than being "safe by spec." This is a server-backing-store
property invisible from the SMB protocol itself.
Source: general NTFS/FAT feature-support summaries, e.g.
https://learn.microsoft.com/en-us/windows/win32/fileio/filesystem-functionality-comparison

Practical implication: cifs.ko's link(2) is safe as create-if-absent
against a proper NTFS-backed SMB2/3 share (Windows Server, Samba on a
Linux filesystem with hard link support, etc.), unsafe/unsupported against
a FAT/exFAT-backed share, and its correctness against any other SMB
server implementation (non-Samba, non-Windows NAS firmware) depends on that
server correctly honoring ReplaceIfExists=FALSE — this is a MS-FSA
"SHOULD/MUST" contract, not something Linux can verify itself.

## FUSE-backed filesystems

Classification: C by default (depends entirely on the specific FUSE
filesystem), can be A only for a specific FUSE filesystem you have
verified honors the contract; the kernel-side plumbing does not add its
own atomicity guarantee beyond "route the call, one at a time."

FUSE's link callback in fuse_operations is minimally documented: "Create a
hard link to a file," with no protocol-level atomicity guarantee attached
to it in the API contract itself.
Source: https://raw.githubusercontent.com/libfuse/libfuse/master/include/fuse.h
(doc comment on the link member of fuse_operations)

For a FUSE mount used only from the local machine, the kernel VFS layer
still takes the same parent-directory exclusive lock described above
before calling down into the FUSE filesystem, so two local processes
racing link() to the same newpath on the same FUSE mount cannot both get a
"success" from the kernel's perspective — the loser gets EEXIST from the
VFS dentry cache before the FUSE daemon is even asked, for names the
kernel already has cached, or the FUSE daemon's own link handler decides
based on its own lookup. The load-bearing question is what the FUSE
filesystem's link handler actually does against its backing store: if that
backing store is itself local and the handler performs an atomic
create-exclusive against it, you get real atomicity; if the handler
does its own "check if exists, then create" as two separate steps against
a remote or eventually-consistent backend (common in FUSE filesystems that
front object stores, SSH, WebDAV, etc.), or if two different machines each
run their own FUSE mount against the same shared remote backend (so the
Linux kernel's local dentry-cache/inode-lock guarantee does not span
machines at all), the atomicity and EEXIST-safety are only as good as that
specific filesystem's implementation — there is no cross-machine or
cross-mount guarantee from FUSE or the kernel at all.

Practical implication: treat "runs over FUSE" as disqualifying by default
for a distributed idempotency primitive unless you have read that specific
FUSE filesystem's link() implementation and confirmed it performs an
atomic exclusive-create against its backing store (not a separate
stat-then-link), and confirmed all writers are going through server-side
mediation rather than independent local FUSE mounts of a shared backend.

## overlayfs

Classification: C for the general "link across the lower/upper boundary"
case as a create-if-absent primitive — not because EEXIST becomes unsafe,
but because a hard link operation itself can trigger a mutating copy-up
of the source file, which breaks the invariant that link() only touches
directory-entry metadata; A only for links where both oldpath and newpath
already resolve to the upper layer (i.e. the source file has already been
copied up, or was created directly in the upper layer) — in that case it
behaves like the underlying upper filesystem.

overlayfs documentation states plainly that creating a hard link forces a
copy-up: "creating a hard-link also requires copy_up, though of course
creation of a symlink does not." Copy-up means the kernel materializes a
private copy of the (previously lower-only) source file into the upper
filesystem before it can create the new directory entry there — this is a
data-copying side effect that a caller relying on link() as a cheap,
metadata-only, atomic operation would not expect.
Source: https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html

Link-count/identity breakage: without the "index" feature enabled, copying
up a file that has multiple existing hard links to it breaks the
relationship between the names: "If this feature is disabled and a file
with multiple hard links is copied up, then this will 'break' the link.
Changes will not be propagated to other names referring to the same
inode." That means if newpath's directory entry ends up copied up while
oldpath's other names remain on the lower layer, subsequent writes through
one name will not be visible through the other — the filesystem silently
stops behaving like one inode with N links and starts behaving like N
independent files. For a distributed idempotency check that relies on "if
two names both point at one inode, only one link() could have won," this
is a correctness hazard specifically because it can quietly convert what
looks like a successful, safe hard link into two unrelated files with the
same content, snapshotted at copy-up time, rather than one shared inode.
Source: https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html
(index feature section)

Inode-identity caveat compounding this: overlayfs states "st_ino will only
be unique when combined with st_dev, and both of these can change over the
lifetime of a non-directory object" unless the xino feature is enabled to
compose a stable identifier. A verification step that stat()s newpath and
compares st_ino/st_dev against oldpath (the natural way to confirm a hard
link really happened, and the same technique the NFS lockfile idiom
relies on) can therefore observe values that later change, unless xino is
in effect.
Source: https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html
(st_ino/st_dev and xino discussion)

Practical implication: do not treat link() on an overlayfs mount as a
lightweight metadata operation, and do not treat post-link stat()-based
identity verification as stable unless the index and xino features are
both enabled. If oldpath is guaranteed to already live in the upper layer
(e.g. it was itself just created there, never coming from a lower layer),
the copy-up hazard does not apply and overlayfs behaves like whatever
filesystem backs the upper directory.

## Bind mounts and same-machine mount namespaces

Classification: A, behaves exactly like the underlying filesystem — with
one specific, well-documented exception.

A bind mount of a directory is the same vfsmount pointing at the same
dentries/inodes on the same superblock; operations inside a single bind
mount are indistinguishable from operating on the original mount, so
link()'s atomicity and EEXIST-safety are exactly whatever the underlying
filesystem provides (ext4: A, tmpfs: A, etc.). Mount namespaces likewise do
not change per-filesystem behavior; they change what is visible/mounted
where, not how the underlying filesystem implements ->link.

The exception, already noted above: link() checks that oldpath and newpath
resolve through the same vfsmount object, not merely the same underlying
superblock. If the same filesystem (or the same directory) is bind-mounted
at two different mount points, and oldpath/newpath are resolved through
two different mount points, the kernel returns EXDEV even though it is
provably the same filesystem/inode-space underneath. This was raised as an
explicit feature request to relax to a same-superblock check and rejected
by maintainers, because the current mount-based check is deliberately used
as a confinement boundary (e.g. `mount --bind /tmp /tmp` to stop something
writable-in-/tmp from hard-linking arbitrary files elsewhere on the root
filesystem).
Source: https://lkml.iu.edu/hypermail/linux/kernel/0712.2/1169.html and the
surrounding thread (maintainer responses at
https://lkml.iu.edu/hypermail/linux/kernel/0712.2/1175.html and
https://lkml.iu.edu/hypermail/linux/kernel/0712.2/1262.html)

Practical implication: if your two paths (oldpath, newpath) might resolve
through two different bind mounts of what is "the same" filesystem, do not
assume link() will work at all — you will get EXDEV, not success and not
EEXIST. This is a mount-topology footgun, not an atomicity or safety
problem: when link() does succeed within one mount, it is exactly as safe
as the underlying filesystem.

## Cross-machine / shared network storage in general

Classification: B at best, and only with specific engineering around the
failure-to-observe-the-reply problem; never treat a bare "link() returned
nonzero" as proof of non-creation.

Across the filesystem types actually usable from more than one machine at
once (NFS, SMB, and any FUSE filesystem fronting a shared remote store),
the single universal caveat that supersedes all the per-filesystem detail
above is: the network can drop the reply to a request that the server
already executed. Every protocol here defines LINK/FileLinkInformation as
a single synchronous, server-atomic check-then-create operation (this part
is fine), but a client cannot always tell, from its own local return code,
whether "no reply" means "never executed" or "executed, reply lost." man7
documents this explicitly for NFS and recommends resolving it by stat()ing
the destination and checking the link count, rather than trusting the
link() return code alone.
Source: https://man7.org/linux/man-pages/man2/open.2.html (O_EXCL and NFS
section, and the link()+stat() locking algorithm it describes)

This generalizes as engineering guidance beyond NFS specifically: for any
cross-machine use of link() as an idempotency check, (1) prefer a protocol
version/negotiation that gives you real exactly-once semantics if
available (NFSv4.1+ sessions, per RFC 5661), (2) when that isn't available
or isn't confirmed to be in effect, always treat a failed/timed-out/
errored link() call as "unknown," and resolve the ambiguity by checking
identity server-side (stat both paths and compare st_dev/st_ino, or
compare link count) before deciding whether to retry, and (3) remember
that oldpath and newpath must resolve to the same export/share/mount as
seen by the server — link() across two different exports of what is
"the same" underlying storage is not a supported operation on NFS
(NFS3ERR_XDEV) or, in practice, on SMB (hard links are confined to a
single share/volume by the same NTFS-level same-volume restriction that
applies locally).

## Summary, one line per type

ext4 (local): A. Standard VFS locking, no filesystem-specific caveat.
xfs (local): A. Standard VFS locking, no filesystem-specific caveat.
btrfs (local, same subvolume): A.
btrfs (local, across subvolumes): C. Fails with EXDEV/-EPERM, never
succeeds or overwrites.
tmpfs: A. Standard VFS locking via shmem_link.
NFSv3: B. Safe when you treat network-error/timeout as "unknown, go
stat() and check," not as "did not happen."
NFSv4.0 (no sessions): B. Same crash-ambiguity caveat as NFSv3.
NFSv4.1/4.2 with sessions negotiated: A, with the standard same-export
constraint; sessions give exactly-once semantics that close the NFSv3-
style ambiguity.
CIFS/SMB via Linux cifs.ko against an NTFS-class backing store: B. Safe
specifically because the Linux client hardcodes ReplaceIfExists=0; unsafe
to assume for other SMB client implementations without checking.
CIFS/SMB against FAT/exFAT-backed share: C. Hard links unsupported
outright.
FUSE-backed filesystems: C by default. Safe only for a specific FUSE
filesystem you have personally verified implements an atomic exclusive-
create in its link handler.
overlayfs, link crossing lower/upper boundary: C. Triggers copy-up, a
data-copying side effect, and can silently break link-count/identity
semantics without the index and xino features enabled.
overlayfs, link entirely within the upper layer: A, behaves like the
upper filesystem.
bind mounts, within one mount: A, identical to the underlying filesystem.
bind mounts, across two different mounts of the same filesystem: C.
Fails with EXDEV, does not succeed and does not report EEXIST.
Mount namespaces (same machine): A. No effect on per-filesystem
link() semantics.
Cross-machine shared storage generally: B at best. The single universal
rule is to never treat a failed or ambiguous link() reply as proof of
non-creation; resolve via a follow-up existence/identity check.
