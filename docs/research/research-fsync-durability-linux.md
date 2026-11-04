# fsync(2) durability across Linux filesystems and mount types

Research artifact. Question: after fsync() (file or directory fd) returns
success and the machine then crashes or loses power, what is actually
guaranteed to survive, across ext4, xfs, btrfs, tmpfs, NFSv3/v4, CIFS/SMB,
FUSE, overlayfs, and generic network storage. Compiled from primary sources
(man7.org, kernel.org docs, RFCs, LWN, USENIX) on 2026-08-19.

Classification key used throughout: (a) durable per spec, no special
configuration needed. (b) durable only under a specific, named configuration,
mount option, or kernel version. (c) not reliably durable, do not depend on it.

## Baseline: what fsync(2) and fsync on a directory fd actually promise

Per the Linux man page, fsync() "transfers (\"flushes\") all modified
in-core data of ... the file referred to by the file descriptor fd to the
disk device (or other permanent storage device) so that all changed
information can be retrieved even if the system crashes or is rebooted,"
including a write-through or flush of any disk cache, and it "blocks until
the device reports that the transfer has completed." It also flushes file
metadata. Critically: "Calling fsync() does not necessarily ensure that the
entry in the directory containing the file has also reached disk. For that
an explicit fsync() on a file descriptor for the directory is also needed."
https://www.man7.org/linux/man-pages/man2/fsync.2.html

fdatasync() is the same minus metadata that isn't needed to retrieve the
data (e.g. mtime), but does still flush metadata like file size.
Same source.

POSIX (fsync(3p)) rationale is explicit that the standard cannot force a
hardware guarantee: "fsync() might or might not actually cause data to be
written where it is safe from a power failure," because "buffer cache,"
"physical write," and "non-volatile storage" are implementation-defined. A
conforming implementation is technically allowed to make fsync() a null
operation if _POSIX_SYNCHRONIZED_IO isn't supported.
https://www.man7.org/linux/man-pages/man3/fsync.3p.html

Practical corollary that recurs through every local-filesystem section
below: fsync() only means anything at the physical-media level if the
storage device's write cache is either disabled, non-volatile
(battery/capacitor backed), or the kernel is correctly issuing cache-flush
and FUA (force unit access) commands to it and the device honors them. All
three of ext4, xfs, and btrfs implement this via "barriers," on by default,
and all three document that turning barriers off risks corruption on crash
unless the write cache is itself non-volatile. Sources cited per filesystem
below.

Directory fsync is a real, necessary, and (combined with file fsync, in the
right order) sufficient step for a crash-consistent atomic-publish-via-rename
on ext4, xfs, and btrfs. Necessary because a rename is fundamentally a
directory-entry mutation, and per the man page quote above, an fsync on a
file descriptor does not flush the directory that names it — the directory
inode's own metadata (and, depending on journal state, the log record for
the rename) is a separate durability domain that requires its own fsync.
Sufficient in the sense that fsync(dir) after the rename forces a journal
commit (ext4/xfs) or transaction commit (btrfs) that includes that
directory's current state, so if the fsync(dir) call returns success the
rename is durable. The canonical recipe — used by PostgreSQL's
durable_rename() — is: fsync the old file, fsync the (possibly pre-existing)
target file, rename(2), then fsync the file by its new name AND fsync the
containing directory. PostgreSQL's own source comment for durable_rename in
fd.c documents this exact sequence and states "a crash while this routine is
running will leave you with either the pre-existing or the moved file in
place of the new file; no mixed state or truncated files are possible."
https://doxygen.postgresql.org/fd_8c_source.html
This is corroborated by LWN's writeup of the 2018/2019 filesystem crash
resistance discussions at the Linux Storage, Filesystem and Memory
Management summit: applications commonly (and wrongly) "assume that
renaming a file after setting attributes makes data durable," when in fact
without an explicit fsync of the directory this is not guaranteed on either
ext4 or xfs, and btrfs developers at the time did not initially treat the
gap as a bug. https://lwn.net/Articles/788938/

## ext4

Classification: (b), durable only with the (default) journal and barrier
configuration, and only if the caller does the fsync(file)+fsync(dir)
dance for renames.

Kernel documentation (Documentation/admin-guide/ext4.rst, current at
docs.kernel.org) describes three data journaling modes. data=ordered
(the default): "All data are forced directly out to the main file system
prior to its metadata being committed to the journal" — this is what makes
"fsync a file, the file's data is durable" true in the ordinary case.
data=writeback: "Data ordering is not preserved, data may be written into
the main file system after its metadata has been committed to the
journal" — under this mode a crash between the metadata commit and the
data write can leave stale/prior data behind a file whose size/existence
already looks committed; PostgreSQL and LWN both flag data=writeback as a
configuration where rename-based publish is materially less safe.
data=journal: all data double-written through the journal first, safest
but slowest. https://docs.kernel.org/admin-guide/ext4.html

Barriers: "Write barriers enforce proper on-disk ordering of journal
commits, making volatile disk write caches safe to use, at some performance
penalty." barrier=1 (equivalently the option's default) is what actually
makes an fsync-triggered journal commit correspond to bytes physically past
the disk's volatile write cache. Same source.

auto_da_alloc (default on) exists specifically to patch over applications
that don't fsync before rename: ext4 "will detect the replace-via-rename
and replace-via-truncate patterns and force that any delayed allocation
blocks are allocated such that at the next journal commit... the data
blocks of the new file are forced to disk before the rename() operation is
committed," giving "roughly the same level of guarantees as ext3" and
avoiding the classic "zero-length file after crash" bug from delayed
allocation. This is a heuristic safety net, not a spec guarantee — it does
not replace fsync(dir) as the correct/portable answer, and it does not help
at all for plain (non-rename) new-file creation, which still needs an
explicit fsync of the parent directory. Same source.

Directory fsync on ext4 is real and required: this is precisely what the
man7 fsync(2) NOTES text and the LWN 788938 discussion are about — ext4's
journal commits are per-transaction, and a directory-entry change
(rename/link/create) that hasn't yet been included in a committed
transaction can vanish on crash even though the file's own data was
fsync'd, unless the directory fd is separately fsync'd.

## xfs

Classification: (b), durable with default barrier/logging configuration;
same fsync(file)+fsync(dir) requirement as ext4 for renames.

XFS has write-barrier support enabled by default since kernel 2.6.17,
disabled with the nobarrier mount option; "a write barrier assures file
system consistency on storage hardware that supports flushing of in-memory
data to the underlying device," most importantly for XFS's own journal
sitting on a device with a volatile write-back cache.
https://docs.oracle.com/en/operating-systems/oracle-linux/6/adminsg/about-xfsbarriers.html
Modern XFS combines this with FUA: "For ext4, when doing a journal commit,
first the journal blocks are written, then a REQ_FLUSH, and then a FUA
write of the commit block" — XFS follows the analogous pattern for its own
log, and added a direct fast path where O_DIRECT|O_DSYNC overwrites issue a
FUA write straight to the device instead of a full cache flush, per the
XFS FAQ. https://xfs.org/index.php/XFS_FAQ
Hardware caveat, same as ext4/btrfs: "many hardware RAIDs have a persistent
write cache which is preserved across power failure... this sort of
hardware should report to the operating system that no flushes are
required, and in that case barriers will not be issued, even without the
nobarrier option" — i.e. the OS-level barrier machinery is deferring to
whatever the device honestly reports about its own cache's volatility, and
an SSD/RAID controller that lies about this breaks the guarantee
regardless of filesystem or mount options. Same source.

XFS explicitly documents rename() as not durable on its own: per the LWN
788938 discussion, "renaming a file using rename(2) is not guaranteed to be
durable in face of crashes, especially on filesystems like xfs and ext4
when mounted with data=writeback [ext4-specific]... To be certain that a
rename() atomically replaces file contents in the face of crashes, one must
fsync the old filename, rename the file, fsync the new filename, and fsync
the containing directory." Same directory-fsync mechanics as ext4: fsync on
a file forces XFS's own log to a point that covers that file's
transactions, but a separate directory-entry transaction is only guaranteed
included if the directory fd is separately fsync'd (or synced as a side
effect of the same or a later transaction commit that IS waited on).

## btrfs

Classification: (b), durable with default barrier configuration and
default (off) autodefrag; historically had specific fsync/log-replay bugs
that are worth naming explicitly rather than assuming "COW means always
safe."

Copy-on-write model: modifying data or metadata writes to a new location
and updates the referencing metadata, cascading up the btree to the
superblock, which is the basis of btrfs's crash-consistency design.
btrfs(5) / btrfs mount-options docs, https://btrfs.readthedocs.io/en/latest/ch-mount-options.html

Barriers: default on. "IO write operations are ensured to make it through
the device cache and be stored permanently at the filesystem's consistency
checkpoint" via flush + superblock write + flush. nobarrier: "will most
certainly lead to a corrupted filesystem in case of a crash or power loss,"
except on hardware with a genuinely non-volatile (battery-backed)
write-back cache. Same source.

autodefrag (default off, available since kernel 3.0): detects small random
writes and queues them for background defragmentation. The durability-
relevant caveat is specifically about kernel-version-dependent COW
breakage, not crash safety per se: "defragmenting with Linux kernel
versions < 3.9 or >= 3.14-rc2 ... will break up the reflinks of COW data"
(reflinked copies, snapshots, dedup'd extents), which is a space-usage and
snapshot-independence issue, not a durability-after-crash issue — but it's
the "autodefrag caveat" this question asked about, so noted precisely.
Same source.

flushoncommit (default off): "forces any data dirtied by a write in a
prior transaction to commit as part of the current commit, effectively a
full filesystem sync" — turning it on tightens durability of data that
hasn't itself been fsync'd, at a performance cost; it is not required for
fsync()'d data's durability, which is handled by btrfs's tree log
(equivalent role to ext4/xfs's journal for fsync/O_SYNC writes). Same
source.

fsync via tree log: btrfs uses a lighter-weight "tree log" rather than
full transaction commit to service fsync()/O_SYNC economically; this
mechanism has been the subject of real historical correctness bugs, not
just a documentation footnote — the classic report is data loss where
"after a crash and log tree replay, extents from the beginning of several
files written just before the crash have been missing, though the ends of
files were present and correct," discussed on the linux-btrfs list.
https://www.spinics.net/lists/linux-btrfs/msg43407.html
A separate documented sharp edge: "the tree log is replayed even with a
read-only mount" unless you mount with nologreplay — i.e. simply mounting
a crashed btrfs filesystem read-only to inspect it can itself
apply/complete the pending fsync log, which is a forensics trap more than
a durability failure, but is worth knowing when reasoning about "what
survived the crash." https://btrfs.readthedocs.io/en/latest/ch-mount-options.html

Directory fsync on btrfs: same requirement and same reasoning as
ext4/xfs — rename is a directory-entry-tree mutation, and btrfs's tree log
for a file's fsync does not by itself guarantee the parent directory's
tree-log entry for the rename is included; LWN 788938 notes btrfs
developers did not initially treat this gap (no durability guarantee for
rename without an explicit directory fsync) as a bug at all, in contrast to
ext4/xfs where it was treated as a known, if under-documented, sharp edge.
https://lwn.net/Articles/788938/

## tmpfs

Classification: (c) in the sense that matters to the question — but not
because fsync fails or lies. fsync() on a tmpfs file is implemented as
essentially a null operation (it returns 0/success immediately, there is no
disk write to wait for), because tmpfs's normal backing is RAM, not a block
device — the "durability" question is moot rather than dishonestly
answered: tmpfs contents (outside of swapped-out pages, which land on swap,
not on the tmpfs mount's own namespace) do not survive reboot or power loss
regardless of how many fsync() calls were made, so the fsync no-op is
truthful about the medium's actual persistence, not a bug. This is standard
kernel behavior described in ArchWiki/Gentoo tmpfs documentation and echoed
by kernel source discussion; do not use tmpfs for anything that must
survive a crash, and do not read a successful fsync() on a tmpfs fd as a
durability signal the way you would on ext4/xfs/btrfs.
https://wiki.archlinux.org/title/Tmpfs , https://wiki.gentoo.org/wiki/Tmpfs

## NFSv3

Classification: (b), durable only when the client's fsync completes an
actual COMMIT round-trip to a server that is not itself lying about
"async" writes, and only if you don't hit a soft-mount timeout while that's
happening.

Protocol model (RFC 1813, NFS Version 3 Protocol Specification): writes
carry a stable_how flag of UNSTABLE, DATA_SYNC, or FILE_SYNC. For
FILE_SYNC, "the server must commit the data written plus all file system
metadata to stable storage before returning" from that single WRITE call.
UNSTABLE writes let the server reply before the data is on stable storage
(to allow gathering/coalescing many small writes into fewer, larger disk
writes) — the specification's own philosophy statement (section 1.6) is
that once a synchronous data-modifying call to the server returns, "the
client can assume that the operation has completed and any modified data
associated with the request is now on stable storage." UNSTABLE writes are
explicitly outside that guarantee until committed.
https://www.rfc-editor.org/rfc/rfc1813.html

COMMIT is the mechanism that upgrades previously-UNSTABLE writes to the
same guarantee: "the COMMIT procedure provides the mechanism for flushing
previously asynchronous writes to persistent storage reliably." A COMMIT
reply carries a write verifier; the client compares it against the
verifier it saw on the original WRITE replies. A verifier mismatch is how
the client detects the server rebooted between the WRITE and the COMMIT —
per the spec, "the write verifier is a cookie that the client can use to
determine whether a server has changed instance state ... between a call
to WRITE and a subsequent call to either WRITE or COMMIT," and on mismatch
the client must resend the data (it was never actually safe) rather than
assume it landed. https://www.rfc-editor.org/rfc/rfc1813.html

Linux's client-side fsync() implementation drives exactly this dance: on
fsync (and on close, in the simple case) the NFS client flushes dirty pages
to the server as WRITEs and then issues a single COMMIT; the reply to a
NFS_FILE_SYNC-flagged WRITE, or a successful COMMIT with a matching
verifier, is what the Linux kernel treats as "the data is written to disk
on the server" for the purposes of returning success from fsync(2).
https://utcc.utoronto.ca/~cks/space/blog/unix/NFSWritesAndSync

Where this breaks in practice (matches the question's "silently lost even
after a successful fsync return" framing): the single biggest real-world
gap is server-side, not client-side — the exports(5) "async" export option
is a documented protocol violation: "the async option allows the NFS
server to violate the NFS protocol and reply to requests before any
changes made by that request have been committed to stable storage,"
which means even a client-side fsync()/COMMIT that returns success can be
lying, because the server itself never made the FILE_SYNC/COMMIT promise
true. https://man7.org/linux/man-pages/man5/nfs.5.html

Client mount options that matter: sync vs async (client-side write
buffering policy — under the default/async, "the NFS client delays sending
application writes to the server" until memory pressure, an explicit
sync/msync/fsync, a close, or a lock/unlock event; explicit sync flushes
every write to the server immediately, at a large performance cost but
without changing what fsync() itself guarantees once called) and hard vs
soft (recovery behavior on RPC timeout: hard retries indefinitely, soft or
softerr gives up after retrans retries and returns EIO/ETIMEDOUT to the
caller — the man page's own warning: "a so-called 'soft' timeout can cause
silent data corruption in certain cases," because the application may
treat a failed/incomplete write+commit sequence as done, or a torn
COMMIT may be abandoned mid-verify). https://man7.org/linux/man-pages/man5/nfs.5.html
Practical implication: hard mounts (the effective safe default for
anything you need fsync durability from) will keep retrying the
WRITE/COMMIT sequence across a server reboot rather than silently giving up
— the verifier-mismatch/resend logic above is what makes that safe rather
than merely persistent.

Multi-client visibility (distinct from crash durability, but adjacent):
NFS uses close-to-open cache consistency by default (nocto disables it) —
"file modifications are committed at the time the file is closed; other
clients operating on the file in parallel do not see the changes until the
next time the file is opened." fsync() forces your own client's data to
the server; it says nothing about when or whether another client's
already-open file descriptor will see it. https://man7.org/linux/man-pages/man5/nfs.5.html

## NFSv4 (RFC 7530)

Classification: (b), same core model as NFSv3 (COMMIT + stable storage +
verifier), layered under a stateful, session-oriented protocol.

RFC 7530 defines stable storage in terms of what the server must survive:
"NFSv4 servers must be able to recover without data loss from multiple
power failures ... operating system failures, and hardware failure of
components other than the storage medium itself." It enumerates acceptable
implementations of "stable storage" explicitly, including plain media
commit, battery-backed on-drive cache, server-side battery-backed cache
with recovery software, or a battery/UPS-backed cache plus recovery
software — i.e. the spec allows a write-back cache to count as "stable" as
long as the hardware/software combination survives the enumerated failure
modes, which is the same non-volatility requirement discussed for local
filesystem barriers above, just pushed onto the NFS server's own storage
stack. https://datatracker.ietf.org/doc/html/rfc7530
The COMMIT operation and verifier4 mechanism carry over from NFSv3
conceptually (verifier4 used across COMMIT/CREATE/OPEN/READDIR/WRITE to let
the client detect server state changes such as a reboot). Practical
consequence for fsync(2) on a NFSv4 mount: same shape of guarantee as
NFSv3 — real, but contingent on the server actually implementing COMMIT
correctly and on the client completing the WRITE+COMMIT (or equivalent)
round trip without giving up early on a soft-mounted, unreachable server.

## CIFS/SMB

Classification: (b), durable by default; explicitly weakened by the
nostrictsync mount option.

Per the current kernel CIFS client documentation: "by default cifs will
send an SMB flush request (and wait for a response) on every fsync call,"
which is the SMB-protocol-level durability primitive (roughly analogous to
NFS's COMMIT or a local fsync). The nostrictsync mount option removes that
flush: "when an application does an fsync call then the cifs client does
not send an SMB Flush to the server (to force the server to write all
dirty data for this file immediately to disk)" — though it still sends the
dirty cached data to the server and waits for the server's write
acknowledgment first, so what's lost specifically is the server-side
"actually on disk" guarantee, traded for performance "at a small risk of
[data loss on] server crash." https://docs.kernel.org/admin-guide/cifs/usage.html
Caching/oplock behavior matters too: the CIFS protocol only allows the
client to cache file data locally when it holds an oplock or lease granting
it permission to do so; cache=strict is the kernel default as of 3.7 (using
the kernel pagecache under oplock discipline), and cache=none bypasses the
local cache entirely, going to the server for every read/write. None of
these caching modes change what a successful fsync (with strict sync, the
default) guarantees once it returns; they change how much unflushed data
could still be sitting client-side before that fsync is called.
https://docs.kernel.org/admin-guide/cifs/usage.html

## FUSE-backed filesystems

Classification: (c) as a blanket default — durability is entirely a
property of the specific FUSE daemon's fsync callback, not something the
FUSE kernel module can guarantee on the daemon's behalf. Treat any given
FUSE filesystem as (b) or (c) only after checking that specific
implementation.

The FUSE kernel module dispatches fsync(2) on a FUSE-mounted file to a
userspace `fsync` operation in the filesystem daemon; what happens next
(whether the daemon actually pushes bytes to real stable storage, or
simply returns success) is entirely up to that daemon's own code — there is
no kernel-enforced floor under it the way there is for a native
block-device filesystem's journal/barrier machinery. Real-world instance of
this: the BuildStream project explicitly disabled fsync/fdatasync
passthrough in their SafeHardlinkOps FUSE filesystem layer, i.e. a FUSE
filesystem can and does choose to make fsync a documented no-op for its own
reasons. https://gitlab.com/BuildStream/buildstream/issues/208

Writeback-cache mode (FUSE_WRITEBACK_CACHE, documented in
Documentation/filesystems/fuse-io.rst) is a distinct axis from "does the
daemon honor fsync." In this mode the kernel lets write(2) return as soon
as data hits the kernel-side page cache, deferring the actual FUSE WRITE
message to the daemon until "close(2), fsync(2) and when the last ref to
the file is being released on munmap(2)" (among other implicit triggers
like memory pressure). The kernel docs flag this mode as "generally not
suitable for network filesystems," precisely because it assumes the FUSE
daemon is the only writer of the backing store — multiple writers
(including another node on shared/network-backed storage under a FUSE
front end) can see stale or inconsistent data because the kernel-side page
cache is authoritative until an explicit flush point.
https://www.kernel.org/doc/Documentation/filesystems/fuse-io.txt
Net effect: writeback-cache mode makes fsync() strictly more load-bearing
(it's one of only a few events that push cached writes to the daemon at
all), while doing nothing to guarantee what the daemon does with those
writes once it receives them — the two questions (when does the daemon get
the data, and does the daemon then make it durable) are orthogonal, and
both must be checked per-filesystem.

## overlayfs

Classification: (b), durable for the default (auto) fsync mode's specific
guarantee since the fix landed (mainline commit 641089c1549d, "ovl: fsync
after copy-up," backported to the 4.8 stable series); tunable to a
materially stronger or weaker guarantee via the fsync= mount option; not
durable under the volatile mount option by design.

Historical bug (the "not always properly propagated" issue this question
asked about): before the fix, when overlayfs copies a file up from the
read-only lower layer to the writable upper layer (copy-up), it wrote the
data into a staging file and then renamed it into place, but did not
fsync the staging file's data before that rename. Because "the Linux
filesystem API does not enforce any particular ordering on storing changes
without explicit fsync(2) calls," a crash between the write and the
journal commit that persisted the rename could leave the copied-up file
containing zeros — i.e. the copy-up looked complete (the rename was
visible) but the data behind it hadn't actually reached disk. The fix
added an fsync(2) call on the upper file before the rename/link that
completes the copy-up, "to make the copy up 'atomic'." Upstream commit
641089c1549d8d3df0b047b5de7e9a111362cdce; backported to stable 4.8.
https://lkml.rescloud.iu.edu/1611.1/01307.html
A related, separate regression was reported against overlayfs in kernel
4.13, where PostgreSQL's own directory-fsync-before-any-write pattern
triggered "could not fsync file ... Invalid argument" on an overlayfs root
(squashfs lower + tmpfs upper on a live image); the upstream overlayfs
maintainer's read was that this was PostgreSQL fsyncing a directory it
hadn't yet caused any copy-up/modification in, rather than a
missing-propagation bug per se.
https://www.spinics.net/lists/linux-unionfs/msg03075.html

Current documented behavior (Documentation/filesystems/overlayfs.rst,
current mainline text, retrieved 2026-08-19): overlayfs now exposes an
explicit fsync= mount option with three values. fsync=auto (default):
"call fsync(2) on upper file before completion of data copy up. No
explicit fsync(2) on directory or metadata-only copy up" — i.e. the
zeros-after-crash bug fix, but nothing stronger; a metadata-only copy-up
(e.g. permission change causing copy-up with no data write) or a directory
copy-up is not separately fsync'd by overlayfs itself under auto.
fsync=strict: "call fsync(2) on upper file and directories before
completion of any copy up" — this is the mode to use if you need the
overlay's own internal copy-up/rename operations to be crash-consistent
without you separately fsyncing the upper tree yourself. fsync=volatile
(aliased to the separate "volatile" mount option): skips these syncs
entirely for performance; "volatile mounts are not guaranteed to survive a
crash," and if a writeback error occurs on the upper filesystem after a
volatile mount, "all sync functions will return an error" from then on
(the mount marks itself as having lost the ability to promise anything,
via a $workdir/work/incompat/volatile marker meant to prompt the operator
to discard and recreate the upper/work dirs).
https://www.kernel.org/doc/Documentation/filesystems/overlayfs.rst

Ordinary fsync-by-the-application-on-the-merged-view path: once a file has
been copied up (or was already on the upper layer), fsync(2) on an fd
opened through the overlay merged view is passed through to the real
upper-filesystem file (overlayfs stacks file operations onto the upper
dentry), so it gets whatever durability guarantee that upper filesystem
(commonly ext4 or xfs) gives a normal fsync — including the same
directory-fsync-required-separately caveat discussed above, and the
kernel docs note explicitly that on such single-journal local filesystems
"fsync on a file also persists the parent directory changes ... because
they are usually modified in the same transaction" ONLY for overlayfs's
own internal copy-up rename (which is what fsync=auto relies on to avoid a
second, explicit directory fsync) — this is not a general license to skip
fsync(dir) for your own application-level renames on the merged view; it
describes an implementation detail of overlayfs's own atomic-copy-up
trick, not a portable guarantee. https://docs.kernel.org/filesystems/overlayfs.html

index feature (index=on, or CONFIG_OVERLAY_FS_INDEX=y): relevant to
hardlink and NFS-export correctness rather than fsync durability per se —
"if this feature is disabled and a file with multiple hard links is
copied up, then this will break the link. Changes will not be propagated
to other names referring to the same inode," which is a correctness gap
worth knowing about if durability testing methodology relies on hardlinks
to detect what did/didn't survive a crash on an overlay mount.
https://www.kernel.org/doc/Documentation/filesystems/overlayfs.rst

## Cross-machine network storage, general

What fsync can promise across a network filesystem is bounded by two
separate things that are easy to conflate: (1) the wire protocol's own
durability primitive (NFS COMMIT / FILE_SYNC write, SMB Flush) actually
reaching stable storage on the server, which is a real, spec-defined
guarantee when the server and protocol are both honest (see NFSv3/v4 and
CIFS sections above); and (2) cross-client visibility/consistency, which
fsync says nothing about at all. fsync() only forces your own file
descriptor's data out to the server and (for NFS/CIFS) waits for the
server's acknowledgment; it does not publish a lock, does not invalidate
another client's already-cached read, and does not order your write
against a concurrent write from a different machine. NFS's close-to-open
model is the mechanism that (partially) addresses visibility, and it works
at open/close granularity, not at fsync granularity — a second client that
already has the file open will not see your fsync'd write until it closes
and reopens (or, in NFSv4, until the relevant delegation/lease is recalled).
https://man7.org/linux/man-pages/man5/nfs.5.html

The other network-specific failure mode worth naming directly, because it
is the "silently lost even after successful fsync" case the question
asked about: a server-side configuration lie. NFS's async export option
and CIFS's caching/oplock configuration can each be set up so the server
acknowledges a write (or even a COMMIT/Flush) before the bytes are
actually on its own stable storage — at that point every guarantee above
this layer (including the client's own correctly-implemented fsync/COMMIT
logic) is void, because the false premise (the server told the truth) was
violated below the client's visibility. This is not a client bug and
fsync() has no way to detect it from the client side; it is purely a
server administration/configuration risk, and it's why "durable" for any
network filesystem in this report is conditioned on the server being
correctly configured, not just the client having called fsync() and
gotten success back.

Finally, the general local-filesystem literature on fsync failure
handling applies with extra force to shared/network storage, because
network storage multiplies the number of places a write can fail
asynchronously between your fsync call and the data actually landing.
Two relevant primary sources: the 2018 "PostgreSQL fsync() surprise" saga
documented that on Linux, an fsync() that returns EIO can, on the very
next fsync() call, silently report success for the same unwritten data,
because the kernel had already cleared its internal dirty/error tracking
for those pages after the first failed report — "the retry succeeded,
because the prior fsync() cleared the AS_EIO bad page flag." This was
improved (errseq_t, reporting the error to all fds open at the time of the
failure) starting in Linux 4.13, but the underlying "pages are marked
clean even after a writeback failure" behavior was not fully changed.
https://danluu.com/fsyncgate/ , https://lwn.net/Articles/752063/
The follow-up academic study, Rebello et al., "Can Applications Recover
from fsync Failures?" (USENIX ATC 2020), systematically tested ext4, xfs,
and btrfs under injected I/O failures and found commonalities across all
three (failed pages are marked clean regardless of filesystem, certain
failed block writes render the file/filesystem unusable afterward) and
found that none of five widely used applications (PostgreSQL, LMDB,
LevelDB, SQLite, Redis) had a failure-handling strategy sufficient to avoid
data loss or corruption after an fsync failure.
https://www.usenix.org/conference/atc20/presentation/rebello
Practical implication for this whole report: every "(a)/(b)" classification
above assumes fsync() actually returns success on the first, uninterrupted
try. If your workload has previously seen an I/O error on the same file
(local or networked) and is now getting a clean fsync() return, that clean
return is not reliable evidence of durability — treat any prior I/O error
on a file or filesystem as reason to distrust subsequent fsync() success
on that same file until the application has been restarted / the fd
reopened, per the papers above.
