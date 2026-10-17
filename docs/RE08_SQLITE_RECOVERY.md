# RE-08 — Consistent SQLite snapshot and separate-file recovery (private workspace)

**Scope:** code and offline tests only. No operator root privilege, no public
Nginx changes, no changes to Cloud Reader, and no live VPS restore attempted.

The ReadEra database includes RAW book chapters, revisions, translations,
jobs, credential hashes, browser sessions and audit events. It runs in WAL
mode: copying `jobs.sqlite3` by itself can miss committed WAL data.

## Snapshot on the same host (API + worker may remain live)

Run as the restricted service account, with a pre-existing directory that
belongs to that user and has permissions `0700`:

```bash
python -m chaptera_server.ops_backup snapshot \
  --database /var/lib/chaptera-translator/jobs.sqlite3 \
  --directory /var/lib/chaptera-translator/private-snapshots
```

Uses Python SQLite `Connection.backup` for one consistent point-in-time
image. Performs `PRAGMA integrity_check` and `foreign_key_check` before
publishing a new owner-only SQLite file and its owner-only JSON manifest.
Every snapshot has a fresh random filename. The manifest contains only a
version, basename, UTC creation timestamp, byte count and SHA256, never any
book text, tokens, key digests, prompts or absolute internal paths.

A successful command prints `snapshot=PASS` and the manifest basename.
If the directory is missing, too permissive, symlinked, or any check fails,
the command rejects the operation. No auto-creation of a public backup
directory, no deleting previous snapshots, and no network upload.

**Important:** plaintext snapshots produced by the `ops_backup` module are
sensitive. The separate `ops_backup_crypto` module below can seal them
before transfer. Plaintext snapshots remain sensitive even after sealing.
Keep the filesystem mode `0700/0600`; copy snapshots and manifests to an
independently encrypted, access-controlled, off-host destination under a
separately approved retention policy. Same-disk snapshots are not disaster
recovery. Do not upload raw backups as public GitHub artifacts.

## Verify + restore to a NEW, offline file

```bash
python -m chaptera_server.ops_backup verify \
  --manifest /private/snapshots/readera-...sqlite3.manifest.json

python -m chaptera_server.ops_backup restore \
  --manifest /private/snapshots/readera-...sqlite3.manifest.json \
  --destination /private/restore-candidate/jobs.sqlite3
```

`verify` checks manifest identity, SHA256, size, owner-only permissions,
SQLite integrity and foreign keys. `restore` verifies again and copies to
a fresh destination directory. The target file **must not exist**, and neither
may its `-wal` or `-shm` siblings. Atomic, exclusive publication refuses
overwrite even if a competing process creates the target.

After restoration, examine the separate candidate against an isolated
application instance. **Do not** swap it into the live `jobs.sqlite3`
while the API or worker are running. That deliberate maintenance/rollback
procedure must be reviewed and accepted separately, with both units stopped.

## CI acceptance

`tests/test_ops_backup.py` covers:
- live WAL commits included in the snapshot;
- library + durable jobs/events + moderator password hashes + browser
  sessions recovered together;
- writes after snapshot absent from restored image;
- modified snapshot rejected by SHA256 and zero partial restore output;
- unsafe manifest/path/symlink/permissive directory rejected;
- existing destination and stray `-wal` sidecar never overwritten;
- CLI output does not print private content.

This is a **code gate only**. Operational RE-01/RE-08 still require the
root-owned installer, trusted peer/IP and HTTPS ingress decisions, service
RAM tests, encrypted off-host copy, recurring scheduler, and a witnessed
restore + rollback test on the real VPS. Until that proof exists, do not
mark production as accepted.

## Private peer-IP boundary

The production `chaptera-server serve` entrypoint sets Uvicorn
`proxy_headers=False`. This prevents implicit trust in client-supplied
forwarded-IP headers arriving over a localhost connection. The SQLite login
rate limit therefore uses the connected peer, not a caller-selected value.

When ReadEra is later placed behind a reverse proxy, many clients can share
one visible peer address. This conservative behavior does **not** constitute
finished public-ingress rate limiting: a separately reviewed HTTPS reverse
proxy must strip untrusted inbound forwarding headers and supply verified
client identity, with repeatable end-to-end acceptance tests. Port 8091
remains loopback-only until that gate is complete.

## Encrypted, authenticated bundles for an off-host destination (RE-08-ENC-01)

This package **does not choose or connect a cloud provider**. It creates
self-contained encrypted `.rbe` archives for later operator-controlled
transfer to an independent, access-controlled off-host backup target.

Encryption is AES-256-GCM with a fresh random 96-bit nonce for every bundle,
a 128-bit authentication tag, a fixed format/version and authenticated
metadata (SQLite origin hash, byte count and snapshot identity). Only a
format identifier and nonce are visible before decryption; database contents
and internal manifest metadata are encrypted. The implementation processes
data in bounded 1-MiB chunks and refuses modified, truncated or unknown
formats before publishing the restored database.

The secret is an **existing 32-byte binary key file** supplied through a path.
The key itself must never appear on command lines, in environment variables,
CI artifacts, app logs or Git. Owner-only permissions (`0600`) are mandatory.
Create a 0700 private directory yourself and provision a key **once**:

```bash
python -m chaptera_server.ops_backup_crypto generate-key \
  --path /private/keys/readera-recovery.key

python -m chaptera_server.ops_backup_crypto seal \
  --manifest /private/snapshots/readera-....sqlite3.manifest.json \
  --key-file /private/keys/readera-recovery.key \
  --directory /private/sealed

python -m chaptera_server.ops_backup_crypto unseal \
  --archive /private/sealed/readera-....sqlite3.rbe \
  --key-file /private/keys/readera-recovery.key \
  --destination /private/restore-candidate/jobs.sqlite3
```

The `seal` output directory must be distinct from the plaintext snapshot
directory. Each operation uses a pre-existing 0700, operator-owned
directory, temporary owner-only files, file-system sync, and exclusive
publish. `unseal` authenticates the whole stream and verifies SQLite
integrity/foreign keys before publishing a **new, offline-only** database
at a previously nonexistent path. No key material or book text is printed.

**Key availability is part of disaster recovery:** keep the key in a separate
approved secrets store with an independently tested recovery procedure. Losing
the key means losing access to sealed archives. Do not put the key alongside
archives at the off-host destination. The plaintext snapshot left by
`ops_backup snapshot` is NOT automatically deleted by sealing; define a
separate retention/secure disposal policy. The current code does not schedule
backups, upload to S3/Drive, verify replication to another host, rotate keys or
restore the production database in place.

Operational acceptance is tracked in
[ararra #56](https://github.com/Lalalalendia/ararra/issues/56). Passing
cryptographic tests and rootless VPS smoke is not a substitute for a tested,
encrypted off-host copy and a witnessed real-host recovery.
