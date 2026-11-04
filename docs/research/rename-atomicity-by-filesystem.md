# rename(2)/renameat2 as an atomic publish primitive, by filesystem and mount type

Research question: which Linux filesystem types and mount configurations correctly
support rename(2)/renameat2 with os.replace-style semantics — atomically replacing
an existing destination, or failing atomically, such that a reader never observes a
torn file at the destination and a crash at any point leaves either the old or the
new file intact, never a torn or missing one.

Plain prose, no tables, organized by filesystem/mount type. Each entry ends with a
classification: (a) atomic and crash-consistent by spec, given the stated fsync
discipline, (b) atomic/safe only under a named specific configuration, or (c) not
atomic or not safe.

Two guarantees are being asked about and they are NOT the same guarantee. The first
is visibility atomicity: does a concurrent reader on the same host ever see a
half-written or missing file at the destination path. The second is crash
durability/atomicity: after a crash at an arbitrary point, is the destination
guaranteed to be either the fully-old or the fully-new file, never zero-length,
truncated, or garbage. POSIX and the man pages only promise the first for free.
The second always requires an explicit fsync discipline (fsync the new file's data,
then rename, then fsync the containing directory) even on filesystems where the
first guarantee holds natively.

## POSIX baseline (visibility atomicity only, no durability language)

The Open Group base spec for rename() states in its rationale that the action of
rename() is to be atomic: a successful rename is atomic from the point of view of
the current host — another process will only ever see the file under the old name
or under the new name, never both, never neither.
https://pubs.opengroup.org/onlinepubs/9799919799/functions/rename.html

The normative behavior for replace: "Otherwise, if the directory entry named by new
exists, it shall be removed and old renamed to new." That is the atomic-replace
behavior os.replace() relies on.

Critically, the POSIX text has no normative language at all about crash or power-
failure durability. It only promises the timestamps of the parent directories get
marked for update "upon successful completion." Atomicity-of-visibility is a POSIX
guarantee; atomicity-across-a-crash is not a POSIX guarantee at all — it is a
filesystem-implementation property layered on top, and only actually delivered when
the caller also fsyncs correctly.

man7 rename(2) restates the same visibility guarantee more concretely: "If newpath
already exists, it will be atomically replaced, so that there is no point at which
another process attempting to access newpath will find it missing," with the caveat
that "there will probably be a window in which both oldpath and newpath refer to
the file being renamed" (i.e., a hardlink-visible transitional state, which is fine
for readers opening by name). https://man7.org/linux/man-pages/man2/rename.2.html

The rename(2) man page's BUGS section adds the one filesystem-specific crash caveat
it documents directly: on NFS, if the server performs the rename and then crashes
before replying, the client's retried RPC can appear to "fail" even though the
rename already happened server-side — so on NFS a failed rename() return code does
not reliably mean the rename didn't happen. This is discussed further below.

renameat2(2)'s RENAME_NOREPLACE/RENAME_EXCHANGE/RENAME_WHITEOUT flags require
explicit filesystem support and rolled out per-filesystem over several kernel
releases: ext4 got RENAME_NOREPLACE in Linux 3.15; btrfs, tmpfs, and cifs in 3.17;
xfs in 4.0; and a broad additional set (ext2, minix, reiserfs, jfs, vfat, and
others) in 4.9. A filesystem lacking the flag support returns EINVAL for the flagged
call — the plain unflagged rename() with implicit replace-if-exists semantics is
the older and much more universally supported call. Same URL as above.

## ext4

Visibility atomicity: yes, guaranteed by the VFS/POSIX contract above for a
same-filesystem rename, regardless of journal data mode.

Crash/durability atomicity: this is where ext4 has real, well-documented history,
and the data= mode matters.

Background — ext3's accidental guarantee: with ext3's default data=ordered mode,
file data blocks were forced to disk before the metadata that referenced them was
committed to the journal. In practice this meant the common "write tmp file, then
rename() over the target" pattern was safe on ext3 even without an explicit fsync,
because the data was already down before the rename's metadata could commit. This
was never a documented guarantee, but real applications came to depend on it. LWN's
account: "data=ordered pretty much guarantees that data written to files will
actually be on disk five seconds later" (i.e., at the next commit interval).
https://lwn.net/Articles/322823/

The ext4 regression (2008-2009): ext4 introduced delayed allocation, which defers
even deciding where a file's blocks will live on disk, purely for performance.
Delayed-allocation blocks are not flushed as part of the next journal commit the
way ext3's already-ordered data was; they can sit dirty in memory for up to the
writeback interval (historically up to ~30-60s, commonly cited as a 5s expectation
that broke). If a crash happened after a rename()-based "atomic replace" committed
but before the new file's data had actually been allocated/flushed, the result
after reboot was a zero-length file — silently losing both the old and the new
content. This is the "ext4 zero-length files after crash" incident. Ted Ts'o's
account and the resulting debate about whose bug this was (kernel vs. application)
is at https://lwn.net/Articles/323252/ and https://lwn.net/Articles/322823/ ; a
concrete user report: "After a clean reboot pretty much any file written to by any
application (during the previous boot) was 0 bytes."

The fix — auto_da_alloc: shipped in Linux 2.6.30 (and backported by distros before
that). It is enabled by default on ext4. It does not add a real fsync/ordering
guarantee to the API; instead it is a pattern-detection heuristic: ext4 recognizes
the specific "open(foo.new)/write/close/rename(foo.new,foo)" and
"open(foo,O_TRUNC)/write/close" idioms, and when it sees them, forces immediate
allocation of the delayed-allocation blocks so that in the default data=ordered
mode, the new file's data blocks are guaranteed to hit disk at/before the next
journal commit that includes the rename's metadata. Kernel doc text: "ext4 will
detect the replace-via-rename and replace-via-truncate patterns and force that any
delayed allocation blocks are allocated such that at the next journal commit, in
the default data=ordered mode, the data blocks of the new file are forced to disk
before the rename() operation is committed. This provides roughly the same level of
guarantees as ext3, and avoids the 'zero-length' problem." Verbatim from
https://www.kernel.org/doc/Documentation/filesystems/ext4.txt (auto_da_alloc mount
option; default on).

What this means in practice for the three data= modes on ext4:

data=ordered (default, with default auto_da_alloc=1): the classic
write-tmp/fsync/rename/fsync-dir pattern is fully crash-atomic by spec. Without an
explicit fsync of the new file before rename, you are relying on the auto_da_alloc
heuristic, which covers the exact temp-then-rename idiom but is a best-effort
compatibility shim, not a documented API contract — Ts'o explicitly resisted making
this a hard rename() guarantee: "Changing rename to include significant performance
penalties when it isn't documented or expected to work this way seems like a very
bad idea to me," preferring instead that applications that need it call fsync
explicitly (https://lwn.net/Articles/323252/). The rename itself (the metadata
operation, i.e. the directory-entry swap) still requires an fsync of the
*containing directory* to be durable across a crash — journaling makes the
filesystem's metadata internally consistent after a crash (you never get a
corrupted directory), but it does not mean every committed-looking operation was
actually flushed to the journal on stable storage; an unflushed rename can still be
rolled back by crash recovery if it never reached the journal. The only way to be
sure the rename itself survives a crash is fsync(dirfd) after the rename returns.

data=writeback: metadata consistency is preserved by the journal (no corrupted
filesystem structures after crash) but data-vs-metadata ordering is not preserved.
The auto_da_alloc heuristic still applies for the specific patterns it detects, but
this is the weakest mode — general applications that skip fsync are most exposed to
seeing old, stale, or garbage data after a crash-recovered rename in this mode.

data=journal: both file data and metadata go through the journal, so a rename plus
its associated data commit together; this is the strongest mode and closest to a
by-spec guarantee without needing to lean on the auto_da_alloc heuristic, at a
significant performance cost.

In every mode: fsync() on the file does not by itself guarantee the *directory
entry* (the rename) is durable — a separate fsync of the directory file descriptor
is required, matching the general Linux/POSIX rule that directory metadata changes
need their own fsync. See the fsync/rename discussion at
https://man7.org/linux/man-pages/man2/rename.2.html and the practical writeup at
https://0xkiire.com/crash-consistency-fsync-rename/, which frames it as "rename is
atomic in namespace semantics but not in persistence semantics" — i.e. you get
free visibility atomicity, but durability atomicity is opt-in via fsync.

Classification: (a) atomic and crash-consistent by spec, given the fsync
discipline (fsync new file, rename, fsync directory) — same as any Linux
filesystem. Without that discipline: (b) atomic/safe only under the specific
configuration "data=ordered (default) with auto_da_alloc enabled (default) and the
exact temp-file-then-rename idiom the heuristic detects" — anything outside that
narrow idiom, or data=writeback, falls back to (c) not safe against a crash,
though visibility atomicity (POSIX guarantee (a) in the readers-never-see-torn-file
sense) always holds on ext4 regardless of mode.

## XFS

Visibility atomicity: yes, same VFS-level guarantee as any local Linux filesystem;
same-directory renames are atomic with respect to concurrent readers.

Crash/durability atomicity: XFS uses its own metadata journal (not JBD2) and
per-operation rolling transactions. XFS's own developers have been explicit in
mailing-list discussion that XFS does *not* promise that an unfsynced rename is
durable across a crash purely because it "looked" atomic: "After a crash you could
end up with a 0-sized file... if the filesystem flushes only the metadata about
the rename, but not the contents of the file," and that relying on rename's atomic
*property* without an explicit fsync of both the file and the parent directory is
not sufficient for crash consistency — see the community discussion thread at
https://news.ycombinator.com/item?id=12329274 referencing this exact XFS behavior,
and the XFS-list discussion "is rename atomic in XFS, also some sync question."
XFS's delayed-logging design explicitly distinguishes "atomic from an external
observer's point of view" for a single rolling transaction from durability: a
*chain* of rolling transactions (as can happen for complex renames) is not
guaranteed atomic as a whole if a crash happens mid-chain — recovery replays only
up to the last transaction actually committed to the log. See
https://www.kernel.org/doc/html/v6.1/filesystems/xfs-delayed-logging-design.html.
XFS has no equivalent of ext4's auto_da_alloc heuristic that guesses at the
temp-file-rename idiom — an application on XFS that wants crash-durable publish
must do the fsync(file)/rename()/fsync(dir) sequence itself; there is no
compatibility shim making the naive rename-only pattern safe.

Classification: (a) atomic and crash-consistent by spec, but only given the
explicit fsync discipline — XFS does not provide ext4's default-on heuristic
safety net, so the naive "just rename()" pattern is more exposed than on ext4's
default configuration: (c) not safe without fsync.

## btrfs

Visibility atomicity: yes, same VFS guarantee.

Crash/durability atomicity: btrfs is copy-on-write at the metadata (btree) level,
committing whole-tree snapshots atomically to a new superblock generation roughly
every 30 seconds (or on fsync via the log tree, or O_SYNC/explicit sync). Per
kernel-btrfs discussion, "Btrfs is atomic copy-on-write based, and at each commit
the filesystem is designed to be entirely self-consistent" — after a crash and
remount, the filesystem reverts to the last fully-committed root, so anything
written since either made it entirely into that commit or didn't happen at all
from the filesystem's perspective; there is no possibility of a half-applied
transaction corrupting the tree structure itself
(https://www.spinics.net/lists/linux-btrfs/msg68968.html,
https://lkml.iu.edu/hypermail/linux/kernel/1809.1/05117.html). This gives btrfs
internal metadata-transaction atomicity, but the same caveat as ext4/xfs applies to
whether *your* rename is in that committed generation: without an fsync, a rename
done between the last periodic commit and a crash is simply rolled back — the old
file (or nothing, if it was a fresh create) is what you get back, which is
crash-consistent (never torn) but not durable (your rename can be silently lost,
i.e. you may observe the *old* state after a crash even though the caller thought
the rename() call returned success). btrfs fsync uses a separate, cheaper log-tree
mechanism rather than forcing a full tree commit, and per kernel-list discussion
that log-tree path is also carefully ordered so that overwriting an existing file
via rename is atomic and the new data is guaranteed on disk before the old data is
considered replaced, when fsync is used
(https://lkml.iu.edu/hypermail/linux/kernel/1809.1/05117.html thread, "metadata
operation reordering regards to crash" — explicitly states this reordering-safety
property is shared by ext4, xfs, and btrfs, i.e. none of the three will give you a
"neither old nor new" torn file, only "old" if you skip fsync).

Classification: (a) atomic and crash-consistent by spec given fsync(file);
rename(); fsync(dir) (or an application fsync policy that ensures the containing
transaction/log commits). Without fsync: rollback to the pre-rename state is
possible (you can lose the rename, i.e. observe stale-old data after a crash) but
btrfs's COW design means you will not get a torn/zero-length file the way old
ext3/ext4 delayed-allocation could produce — so unfsynced btrfs sits closer to (b)
"safe against torn files, unsafe against silently reverted renames" than to a flat
(c).

## tmpfs

Visibility atomicity: yes, ordinary VFS rename semantics apply; tmpfs added
renameat2 flag support (RENAME_NOREPLACE, RENAME_WHITEOUT) in Linux 3.17/3.18.
https://man7.org/linux/man-pages/man2/rename.2.html,
https://man7.org/linux/man-pages/man5/tmpfs.5.html

Crash/durability atomicity: not applicable/moot — tmpfs is backed by page cache
and (optionally) swap, with no on-disk persistent structure of its own; a "crash"
(reboot or power loss) always loses the entire tmpfs contents regardless of
rename/fsync. There is no torn-file risk because there is no persistence at all to
tear.

Classification: (a) atomic with respect to concurrent readers (the property this
research question cares most about for a "publish" primitive within a running
system); (c)/not-applicable with respect to crash durability, since tmpfs by
design does not survive a crash at all — if the requirement is "crash leaves either
old or new file intact," tmpfs cannot satisfy it for the trivial reason that
nothing on tmpfs survives a crash.

## NFSv3

Visibility/protocol atomicity, single client: RFC 1813 section 3.3.14 (RENAME)
states plainly: "The RENAME operation must be atomic to the client." RENAME is
also listed among NFSv3's synchronous data-modifying procedures — the server is
required to commit it to stable storage before replying (unlike WRITE, which can be
UNSTABLE and reconciled later via COMMIT): "The following data modifying procedures
are synchronous: WRITE (with stable flag set to FILE_SYNC), CREATE, MKDIR, SYMLINK,
MKNOD, REMOVE, RMDIR, RENAME, LINK, and COMMIT." Full RFC text:
https://www.rfc-editor.org/rfc/rfc1813.html, section detail also at
https://www.freesoft.org/CIE/RFC/1813/34.htm. This is a real durability guarantee
baked into the wire protocol that local-filesystem rename() does NOT get for free:
by spec, when an NFSv3 RENAME reply comes back successful, the server has already
committed it to stable storage — the client does not need a separate fsync of the
directory the way it does locally (though whether a given server actually honors
this synchronous-procedure requirement, e.g. some NAS appliances offering an
"async" export option that violates the RFC for performance, is a
real-world caveat worth checking per-server).

Same-filesystem requirement: RFC 1813 requires "to.dir and from.dir must reside on
the same file system on the server," meaning matching fsid; cross-filesystem
attempts get NFS3ERR_XDEV, mirroring local EXDEV.

Cross-client reality — this is the important nuance the research question is
getting at: RENAME's atomicity is a *server-side, per-request* guarantee — the
server will never leave its own on-disk state torn no matter how many clients hit
it concurrently, since the server executes each client's RENAME as one operation.
So the "publish" itself is genuinely atomic and safe even with multiple writer
machines: two clients racing to rename onto the same destination will not produce
a corrupted destination, they'll produce a well-defined winner (last one the server
processes). What is NOT guaranteed cross-client is *prompt visibility*. NFSv3/NFSv4
clients use close-to-open cache consistency: a client only revalidates a file's
attributes and re-checks the server at open() time (and caches directory contents
for acdirmin..acdirmax seconds, 3-60s by default) — see
https://man7.org/linux/man-pages/man5/nfs.5.html. So client B may keep listing/
resolving the *old* name for several seconds after client A's rename() returned
success on client A, purely due to B's attribute/dentry cache, unless B mounts with
actimeo=0/noac or the caller does an explicit cache-invalidating lookup. A thread on
this exact question ("Atomicity of rename on NFS") exists at
https://lists.archive.carbon60.com/netapp/toasters/16051 (could not be fetched in
this pass due to a network error — flagging as unverified secondary source, but the
same conclusion is corroborated by the primary man7 nfs(5) close-to-open
documentation and RFC 1813's synchronous-procedure text above).

Also worth noting from the rename(2) man page's own BUGS section: on NFS, if the
server performs the rename and then crashes before the RPC reply reaches the
client, the client's automatic retry of the (now already-applied) RENAME can
surface as a spurious failure to the caller even though the rename genuinely
succeeded server-side — so a program cannot safely treat "rename() returned an
error over NFS" as proof nothing changed. https://man7.org/linux/man-pages/man2/rename.2.html

Classification: (b) atomic and safe as a single-writer-server publish primitive,
by spec, including the durability half (RENAME is a synchronous/stable-storage
procedure per RFC 1813) — but "safe with multiple machines" only in the sense that
the destination is never torn; readers on *other* clients are not guaranteed to
observe the rename promptly unless they mount with a tight/zero attribute cache
(actimeo=0, noac) or otherwise force revalidation. Treat NFSv3 rename as
crash-safe-at-the-server but only eventually-consistent in visibility across
independent client machines.

## NFSv4 (v4.0 and v4.1/v4.2)

Protocol structure: RENAME is Operation 29 in NFSv4.0 (RFC 7530, section 16.27) and
its successor RFC 8881 for v4.1. NFSv4 operations are bundled into COMPOUND
procedures; note explicitly that COMPOUND bundling itself does not create
cross-operation atomicity — this is stated directly in RFC 7530 discussing
filehandle-expiry recovery: "The COMPOUND procedure does not provide atomicity"
(this refers to combining multiple distinct operations in one COMPOUND request, not
to the RENAME operation's own internal atomicity, which is still a per-operation
guarantee inherited from the same NFS design lineage as NFSv3).
https://datatracker.ietf.org/doc/html/rfc7530

Caching model: RFC 7530's client-side caching section describes the same
close-to-open-style model as NFSv3 — "Attributes and directory information are
cached for a duration determined by the client. At the end of a predefined
timeout, the client will query the server to determine if the file has been
changed," and file data cache validity is checked at open(). NFSv4 adds
*delegations* as an optional mechanism where the server can proactively recall a
client's cached state when another client's operation (including a conflicting
rename in the same directory) would invalidate it — but a delegation is only ever
granted for a single file/open, is optional for the server to hand out, and does
not create a synchronous, guaranteed-instant cross-client rename notification the
way, say, a distributed lock service would. In the absence of a delegation
covering the exact directory in question, the same cache-timeout behavior as
NFSv3 applies: other clients can observe stale directory listings for up to their
configured attribute-cache timeout. Same source, section 10 (client-side caching).

Classification: same as NFSv3 — (b) atomic and crash-safe as the server's own
publish operation (server-side atomicity carries over from the same NFS RPC
design lineage, and NFSv4 metadata operations are likewise not "unstable" writes
that need a separate COMMIT), but cross-client visibility is still bounded by
close-to-open/delegation caching, not instantaneous. Do not treat "I renamed on
client A" as "client B sees it immediately" without forcing revalidation
(actimeo=0/noac, or an explicit re-open/re-lookup) or holding a delegation that
actually covers the affected name.

## CIFS / SMB (Windows protocol, and the Linux cifs.ko client)

This is the one entry on this list where the *protocol's own historical semantics*
differ from POSIX rename, not just the crash/durability story.

Historical Windows restriction: classic Win32/NT file semantics (and thus classic
SMB1/SMB2 FileRenameInformation) did not support atomically renaming onto an
existing filename the way POSIX rename() does. The wire structure used for SMB2
rename, FILE_RENAME_INFORMATION, has a ReplaceIfExists boolean, but per the MS-FSCC
spec, even with ReplaceIfExists=TRUE "the rename operation will still fail if a
file with the same name already exists and is a directory, a read-only file, or a
currently executing file," and critically, a target file with any open handles
against it historically blocked the rename (outside a batch-oplock case).
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fscc/3668ae46-1df5-4656-b481-763877428bcb
This is a materially weaker guarantee than POSIX rename, which allows replacing a
file that still has open file descriptors elsewhere (the old inode just keeps
living until the last descriptor closes, exactly like unlink()). Win32 application
code historically had to reach for MoveFileEx(MOVEFILE_REPLACE_EXISTING) or
ReplaceFile() to even get overwrite-on-rename, and true atomicity of that
replace was long ambiguous/undocumented — Windows 10 (RS1) added
FileRenameInformationEx with an explicit FILE_RENAME_FLAG_POSIX_SEMANTICS flag
specifically to let a rename replace a target even while it still has open
handles, matching POSIX unlink-then-replace behavior for the first time as a
first-class, documented mechanism (per NtDoc/ntifs.h structure documentation
surfaced via community sources; treat the exact RS1 version-gating as
secondary-sourced, but the documented FILE_RENAME_INFORMATION structure and its
Ex/Flags variant are primary from learn.microsoft.com).

SMB3.1.1 POSIX extensions and the Linux client: Linux's cifs.ko client (mainline
kernel), Samba's smbclient, and server-side ksmbd/Samba added "SMB3.1.1 POSIX
extensions," which, when negotiated (mount option "posix" on the Linux client,
requiring kernel 5.8+ with substantial improvements through 6.13, per Samba wiki),
change rename to actually behave POSIX-like: "POSIX rename(2) ignores other POSIX
opens" — i.e., once this extension is negotiated end-to-end (Linux client, and a
server that supports it — Samba or ksmbd; NOT a plain Windows server, which does
not speak the POSIX extension), rename-over-open-file and atomic replace behave
like a native POSIX filesystem. Sources: Samba wiki
https://wiki.samba.org/index.php/SMB3-Linux ("renaming a directory that has open
files [is] perfectly legal in POSIX but not in Windows... Emulatable over SMB3 for
most cases"; kernel 3.18+ improves this, POSIX Create Context still needed for some
cases) and https://smb3posix.org/.

Without the POSIX extension (i.e., talking to a stock Windows server, or an SMB1/
plain-SMB2 dialect, or a Linux client mounted without "posix"), the underlying
protocol's ReplaceIfExists rename is close to POSIX rename in the common case
(target exists, no open handles, not a directory, not read-only) but is not a
strict guarantee-for-all-cases replacement, and has historically had edge-case
failures around open handles and directories that POSIX rename does not have.
Durability: SMB is itself a network protocol with its own ack/durable-handle
machinery (see SMB durable/persistent handles and continuous availability
features), but there is no widely-documented, protocol-level "RENAME is a
synchronous/stable-storage procedure" text analogous to NFSv3's RFC 1813 wording
found in this pass of research — treat SMB rename durability-after-server-crash as
implementation-defined per server (Windows Server, Samba, ksmbd, or a NAS vendor's
CIFS head), not protocol-guaranteed the way NFSv3 explicitly documents it.

Classification: (b) atomic/safe only under a specific configuration — SMB3.1.1
POSIX extensions negotiated end-to-end (Linux cifs.ko client with "posix" mount
option against a POSIX-extension-aware server such as recent Samba/ksmbd). Plain
SMB2/SMB3 without POSIX extensions, or against a stock Windows server, or SMB1: (c)
not the same atomic-replace guarantee as POSIX rename — target-exists-with-open-
handles and directory-rename cases can fail or behave differently than os.replace()
callers expect, even though the simple "no one has it open" case is close to POSIX
in practice.

## FUSE-backed filesystems

There is no single answer here by design — rename atomicity through FUSE is
delegated entirely to the userspace filesystem's own implementation, and the
kernel FUSE layer only defines the *contract* the filesystem implementation is
expected to honor, not an enforcement mechanism.

libfuse's low-level rename callback documentation states the contract plainly: "If
the target exists it should be atomically replaced," and if the target inode's
in-kernel lookup count is non-zero, "the file system is expected to postpone any
removal of the inode until the lookup count reaches zero" (i.e., it must implement
the same "still-open files keep working" semantics native Unix filesystems give
for free). Source: libfuse's fuse_lowlevel.h,
https://github.com/libfuse/libfuse/blob/master/include/fuse_lowlevel.h. FUSE also
passes through the renameat2 flags (RENAME_EXCHANGE, RENAME_NOREPLACE) to the
filesystem implementation, again as a "should/must" contractual obligation on that
implementation, not something the FUSE kernel module enforces on its behalf.

Because "the filesystem implementation" for a FUSE mount can be anything from a
thin passthrough over a local ext4 directory (which would inherit ext4's actual
guarantees), to an S3/object-store bridge (where "rename" is frequently
emulated as copy+delete and is neither atomic nor crash-consistent — a reader can
see a missing object mid-operation, and a crash mid-copy can leave neither old nor
new data), to an encrypted-overlay or network-backed filesystem with its own
consistency model entirely, there is no blanket claim possible. A caller must
verify the specific FUSE filesystem's own documentation/source for whether it (a)
implements rename as a true atomic rename in its backing store, (b) emulates it
non-atomically, or (c) doesn't support atomic replace at all (some FUSE
filesystems, notably many object-storage gateways, explicitly do NOT support
atomic rename-over-existing and this is a commonly hit limitation).

Classification: cannot be classified in general — entirely dependent on the
specific FUSE filesystem. Verify per-implementation before relying on it as a
publish primitive; do not assume FUSE mounts behave like local ext4/xfs/btrfs.

## overlayfs

Same-layer file rename (both source and destination already materialized on the
upper, writable layer, non-directory): this passes through to a real rename() on
the upper filesystem, so it inherits that upper filesystem's atomicity and crash
properties exactly (e.g., upper=ext4 behaves like the ext4 entry above). Kernel
docs describe that once copy-up is complete for a file, "the overlay filesystem
simply provides direct access to the newly created file in the upper filesystem" —
i.e. subsequent operations including rename are native operations on the upper fs.
https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html

Renaming a file that still only exists on a lower (read-only) layer: overlayfs must
first copy the file up to the upper layer (copy-up), then perform the rename on the
upper layer. Copy-up itself is not a single atomic kernel operation from the
caller's point of view — it is a sequence (create upper file, copy data, set
attributes/xattrs) and the overlayfs implementation is documented to be careful
about fsync/ordering during copy-up for its own crash-consistency, but this makes
"renaming a not-yet-copied-up lower file" a substantively different, heavier-weight
operation than a plain rename on a native filesystem — treat it as not equivalent
to a single atomic rename() for crash-consistency purposes unless you've confirmed
the overlayfs version's copy-up-then-rename sequencing/fsync behavior.

Directory rename, the historically restricted case: by default, renaming a
directory that lives (fully or partially) on a lower layer fails outright with
EXDEV — "this error is returned by rename(2) when trying to move a file or
directory across filesystem boundaries" — because overlayfs historically could not
represent "this upper directory used to be at a different lower path" at all.
Kernel doc: https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html. The
redirect_dir feature (mount option redirect_dir=on/follow/nofollow/off,
CONFIG_OVERLAY_FS_REDIRECT_DIR) lifts this by copying the directory up (metadata
only, not recursive contents) and recording the original lower path in a
"trusted.overlay.redirect" xattr, letting the rename appear to succeed — but note
this is documented as backward-incompatible (older kernels cannot correctly mount
an overlay containing redirected directories) and it is explicitly a multi-step
process (copy-up + xattr + move), not a single atomic filesystem-level rename.

Classification: (b) atomic/safe only under the specific configuration "rename
targets are both already on the upper layer (no lower-layer copy-up involved)," in
which case atomicity/crash-consistency equals whatever the upper filesystem
provides (e.g. ext4/xfs/btrfs as documented above, with the same fsync
requirements). Directory renames spanning lower/upper layers without redirect_dir
enabled are (c) not supported at all (EXDEV); with redirect_dir enabled they
succeed but are not a single atomic operation the way a native rename is — treat as
(b) under that named configuration with weaker atomicity properties than a plain
local rename.

## Bind mounts

A bind mount (mount --bind, or the bind mount option) makes an existing directory
subtree visible at another path; per mount(8), it does not create a new
filesystem instance — files accessed through the bind mount are the same
inodes/blocks as through the original path, on the same underlying filesystem.
https://man7.org/linux/man-pages/man8/mount.8.html. It is even possible for the
read-only/read-write mount flag to differ between the bind mount and the original
mount point (since Linux 2.6.26, "-o bind,rw" can grant read-write on a bind mount
of a read-only-mounted source path via a mount-namespace-level flag), but this
affects permission enforcement only, not the underlying filesystem's rename
semantics.

Because a bind mount is not a distinct filesystem, a rename() performed through it
is literally the same operation, on the same underlying filesystem, that would
happen via the original path — there is no additional layer, translation, or
network hop introduced. One real caveat: rename()'s same-filesystem requirement is
about the underlying device/superblock, not the mount-namespace path — renaming
between two paths that resolve to the same underlying filesystem via different bind
mounts is fine (same as any other same-filesystem rename), but renaming between two
paths that happen to be bind-mounted from *different* underlying filesystems into
what looks like one tree will still hit EXDEV, exactly as it would without the bind
mounts, because rename(2)'s cross-device check is based on the actual backing
device/superblock, not the path layout.

Classification: not a distinct case — (a)/(b)/(c) exactly mirrors whatever the
underlying filesystem entry above says, with no additional caveats introduced by
the bind mount itself.

## Cross-machine network storage in general: the core caveat

Pulling the NFS and CIFS specifics above into a general statement, since the
research question specifically asks about relying on rename()'s atomicity as a
crash-consistent publish primitive when *multiple machines* write to the same
shared network filesystem:

The atomicity guarantee that matters (no reader ever observes a torn destination)
is, for protocols that specify it correctly (NFSv3/v4 per RFC 1813/7530/8881, and
SMB when the POSIX extensions are negotiated), a *server-side* guarantee: the
server executes each RENAME as one operation against its own backing store, so
concurrent renames from different client machines onto the same destination path
are serialized by the server and never produce a torn result. In that narrow
sense, rename-as-publish is genuinely safe across multiple writer machines, not
just single-client-safe — this is a protocol-level guarantee, not just a local
kernel VFS guarantee.

What is NOT a cross-machine guarantee is *prompt, or even bounded, visibility*.
Every network filesystem examined here (NFSv3, NFSv4, and CIFS/SMB to varying
degrees) uses client-side caching for performance — close-to-open consistency on
NFS (revalidate at open(), cache directory listings for several seconds by
default), oplocks/leases on SMB — so a second machine can continue to observe the
*old* file, or a stale directory listing that doesn't show the new file at all,
for a bounded-but-nonzero window after the rename already completed and was
acknowledged to the renaming client. This is the practical trap in treating
rename() as a distributed "publish" primitive: the operation itself doesn't tear,
but "everyone sees it at the same time" is not promised, and depending on mount
options (actimeo/noac on NFS; oplock/lease behavior on SMB) that window can be
several seconds to tens of seconds by default.

The second general caveat is about failure semantics specifically introduced by
the network: rename(2)'s own BUGS documentation calls this out for NFS by name —
if the server-side rename genuinely succeeds but the acknowledgement is lost (e.g.
server crashes right after completing the rename, before the RPC reply reaches the
client), the client can observe what looks like a failed call for an operation that
actually did happen. A caller that treats "rename() returned an error" as proof of
"nothing changed" can be wrong over a network filesystem in a way it would never
be wrong locally. https://man7.org/linux/man-pages/man2/rename.2.html

Practical implication for "atomic publish" patterns built to run against a network
filesystem shared by multiple machines: the atomic-replace-never-torn guarantee
generally holds (assuming a spec-compliant server and, for SMB, POSIX extensions
negotiated), but (1) durability-after-server-crash is protocol-guaranteed for
NFSv3/v4 (RENAME is a synchronous, must-commit-before-reply procedure) but not
documented the same way for CIFS/SMB, where it is server-implementation-dependent;
and (2) cross-client read-your-(someone-else's)-writes visibility is bounded by
cache-consistency settings, not instantaneous, so a design that assumes "as soon as
client A's rename() returns, client B's next read will see the new file" needs an
explicit cache-busting mechanism (fresh lookup, actimeo=0/noac, or equivalent) — it
is not safe to assume by default.

## Summary classification list (plain lines)

ext4, data=ordered (default) with fsync(file)+rename+fsync(dir): atomic and crash-consistent by spec.
ext4, data=ordered (default), no fsync, exact temp-then-rename idiom: safe only via the auto_da_alloc heuristic (default-on), not a documented API guarantee.
ext4, data=writeback, no fsync: not safe against crash; visibility atomicity still holds.
ext4, data=journal: atomic and crash-consistent, strongest mode, at a performance cost.
XFS, with fsync(file)+rename+fsync(dir): atomic and crash-consistent by spec.
XFS, no fsync: not safe against crash (no auto_da_alloc-equivalent heuristic); visibility atomicity still holds.
btrfs, with fsync (or fsync-equivalent commit): atomic and crash-consistent by spec.
btrfs, no fsync: rename can be rolled back to prior state after a crash (old file reappears), but not torn — safer than unfsynced ext4/xfs against torn files, still unsafe against silently-lost renames.
tmpfs: atomic with respect to concurrent readers; crash durability is moot, tmpfs does not survive a crash at all.
NFSv3: atomic and crash-consistent at the server by protocol spec (RENAME is a synchronous, must-commit procedure per RFC 1813); cross-client visibility is only eventually consistent, bounded by close-to-open/attribute-cache settings.
NFSv4 (4.0/4.1/4.2): same as NFSv3 — server-side atomic and durable by spec; cross-client visibility bounded by close-to-open caching and optional, non-guaranteed delegations.
CIFS/SMB with SMB3.1.1 POSIX extensions negotiated (Linux cifs.ko "posix" mount option, POSIX-aware server such as recent Samba/ksmbd): atomic/safe, matching POSIX semantics including rename-over-open-handles.
CIFS/SMB without POSIX extensions (plain SMB2/SMB3, SMB1, or a stock Windows server): not the same guarantee as POSIX rename — historically blocks or behaves differently when the target has open handles or is a directory; ReplaceIfExists covers the simple no-open-handles case reasonably but is not a strict equivalent.
FUSE-backed filesystems: unclassifiable in general — entirely dependent on the specific implementation; verify per filesystem, some (e.g. many object-storage-backed FUSE gateways) do not support atomic rename-replace at all.
overlayfs, same-layer (already-upper) file rename: inherits whatever the upper filesystem provides (see ext4/xfs/btrfs entries above), given the same fsync discipline.
overlayfs, rename involving a lower-layer file (needs copy-up): heavier, multi-step operation; not equivalent to a single atomic rename() for crash-consistency purposes.
overlayfs, directory rename across lower/upper without redirect_dir: not supported, fails with EXDEV.
overlayfs, directory rename with redirect_dir=on: succeeds but via a multi-step copy-up-plus-xattr-plus-move sequence, not a single atomic operation.
bind mounts: no distinct behavior — exactly mirrors whatever the underlying filesystem provides.
Cross-machine shared network storage in general: the no-torn-destination guarantee generally holds server-side across concurrent writer machines for spec-compliant NFS and POSIX-extension SMB; durability-after-server-crash is protocol-guaranteed for NFSv3/v4, implementation-dependent for CIFS/SMB; prompt cross-client visibility of the rename is never guaranteed by default on any of these, only eventual visibility bounded by client-side cache-consistency settings.

## Primary sources cited

POSIX rename(): https://pubs.opengroup.org/onlinepubs/9799919799/functions/rename.html
man7 rename(2)/renameat2(2): https://man7.org/linux/man-pages/man2/rename.2.html
man7 mount(8), bind mounts: https://man7.org/linux/man-pages/man8/mount.8.html
man7 nfs(5), attribute cache options: https://man7.org/linux/man-pages/man5/nfs.5.html
man7 tmpfs(5): https://man7.org/linux/man-pages/man5/tmpfs.5.html
ext4 kernel docs (auto_da_alloc, data= modes): https://www.kernel.org/doc/Documentation/filesystems/ext4.txt and https://www.kernel.org/doc/html/latest/filesystems/ext4/journal.html
ext4 zero-length-file history: https://lwn.net/Articles/322823/ and https://lwn.net/Articles/323252/
XFS delayed logging design (rolling-transaction atomicity vs durability): https://www.kernel.org/doc/html/v6.1/filesystems/xfs-delayed-logging-design.html
XFS rename/fsync discussion: https://news.ycombinator.com/item?id=12329274
btrfs COW commit/crash-consistency discussion: https://www.spinics.net/lists/linux-btrfs/msg68968.html and https://lkml.iu.edu/hypermail/linux/kernel/1809.1/05117.html
RFC 1813 (NFSv3), RENAME procedure and synchronous-procedures list: https://www.rfc-editor.org/rfc/rfc1813.html
RFC 7530 (NFSv4.0), RENAME operation and client-side caching: https://datatracker.ietf.org/doc/html/rfc7530
RFC 8881 (NFSv4.1): https://datatracker.ietf.org/doc/html/rfc8881
MS-FSCC FileRenameInformation (ReplaceIfExists semantics and restrictions): https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fscc/3668ae46-1df5-4656-b481-763877428bcb
Samba SMB3-Linux wiki (POSIX extensions, rename-with-open-files): https://wiki.samba.org/index.php/SMB3-Linux
SMB3 POSIX extensions spec site: https://smb3posix.org/
libfuse fuse_lowlevel.h (rename contract): https://github.com/libfuse/libfuse/blob/master/include/fuse_lowlevel.h
Kernel overlayfs docs (EXDEV on directory rename, redirect_dir, copy-up): https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html

## Notes on sourcing gaps / lower-confidence claims

The NetApp "toasters" mailing-list thread on cross-client rename atomicity
(https://lists.archive.carbon60.com/netapp/toasters/16051) could not be fetched in
this research pass (connection error) — it is cited above only as a pointer for
further reading, not as a verified source; the conclusions attributed to the
general cross-client visibility discussion instead rest on the man7 nfs(5) and RFC
7530 primary sources, which is a solid basis independent of that thread.

The Windows 10 RS1 FileRenameInformationEx/FILE_RENAME_FLAG_POSIX_SEMANTICS
version-gating detail came through secondary community documentation
(ntdoc.m417z.com) rather than a Microsoft primary source fetched directly in this
pass; the existence and behavior of the FILE_RENAME_INFORMATION structure and its
ReplaceIfExists field is primary-sourced (MS-FSCC), but the exact Windows-version
introduction of the Ex/POSIX-semantics variant should be treated as secondary-
sourced and worth double-checking against MS-FSCC/MS-FSA revision history directly
if it becomes load-bearing for a decision.

XFS's own documentation was fetched but did not surface a single canonical
"XFS rename crash-atomicity" statement as explicit as ext4's auto_da_alloc kernel-
doc text; the XFS position here is synthesized from the delayed-logging design doc
plus community/mailing-list discussion rather than one authoritative kernel-doc
paragraph — flagged because it's the one filesystem in this report without a single
clean primary-source quote covering the exact "do I need fsync" question the way
ext4 and NFS have.
