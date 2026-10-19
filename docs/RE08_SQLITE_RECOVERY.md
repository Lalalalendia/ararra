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

### RE-08-VAULT-ESCROW-01 — second key needed after disaster restore

**Two different encryption secrets are required** when the restored SQLite
contains moderators' encrypted Gemini project API keys:

1. The **binary 32-byte recovery key** for the off-host `.rbe` archive
   (`ops_backup_crypto`). It restores the SQLite bytes, *not* personal Gemini keys.
2. The **Base64-encoded 32-byte `gemini_vault_key`** that the restricted
   API and worker load from protected systemd credentials. This credential
   is created during the one-time host bootstrap and is **not inside SQLite,
   `.rbe`, the S3 object or the release wheelhouse**. If it is lost, project
   credentials already stored in the recovered SQLite cannot be decrypted.
   Operators would have to revoke and re-enter those keys after restoring
   books/jobs. No automatic vault-key regeneration or silent rotation is safe.

Both secrets require **separately approved, independently retrievable
off-host escrow**; do not store either in Git, CI artifacts, Notion, public
logs, the S3 ciphertext bucket or beside an unprotected plaintext database.
The `team_tokens_json` systemd credential and any protected OAuth credentials
needed for actual sign-in/Google Drive are also independent of the SQLite
archive. Recovering just the database is not full production recovery.

After downloading an authentic S3 `.rbe` and using the separately recovered
RBE key to `unseal` into a **NEW, offline, operator-owned 0700 directory**,
copy the independent Gemini vault credential into a *different* private 0700
directory, with a **0600** file. Then run:

```bash
python -m chaptera_server.ops_backup_vault \\
  --database /private/offline-recovery/jobs.sqlite3 \\
  --vault-key-file /private/escrow-witness/gemini_vault_key
```

This read-only witness validates SQLite integrity, standalone journal mode
and every saved Gemini project using authenticated decryption and keyed
fingerprints. It prints only `vault_escrow=PASS` and
`projects_checked=<count>`. It returns a nonzero code and redacted
`vault_escrow=REJECTED` if a key is missing/incorrect, a credential is
corrupted, no encrypted credentials exist to prove the key, or the target
could be a live WAL database. It **never outputs recovered API keys**,
master key bytes, identities, book text or absolute private paths.

The acceptance still requires **independent host and identity**, proof of
sealed object retention, validated Reader/API behavior, team-key and OAuth
credential availability and operator-reviewed rollback. An offline CI test
using disposable keys does not satisfy the live disaster-recovery gate.

Operational acceptance is tracked in
[ararra #56](https://github.com/Lalalalendia/ararra/issues/56). Passing
cryptographic tests and rootless VPS smoke is not a substitute for a tested,
encrypted off-host copy and a witnessed real-host recovery.

## RE-08-OFFHOST-S3-01 — encrypted off-host S3 storage

The optional S3 transport accepts **only** encrypted `.rbe` bundles from
`chaptera_server.ops_backup_crypto seal`. It does not accept plaintext
SQLite, snapshot manifests, API credentials or the master recovery key.
Install only for the operator CLI:

`python -m pip install 'chaptera-server-translator[offsite-s3]'`.

Credentials are resolved by boto3 from the normal IAM/profile/role chain,
never passed as command-line arguments. Restrict the IAM principal to the
private bucket and vault prefix, with no delete permission. Configure an
independently managed retention/immutability policy on the S3 bucket.

```bash
python -m chaptera_server.ops_backup_s3 \
  --bucket PRIVATE_BUCKET --vault readera-vault-01 --region us-east-1 \
  push --archive /private/sealed/readera-...sqlite3.rbe

python -m chaptera_server.ops_backup_s3 \
  --bucket PRIVATE_BUCKET --vault readera-vault-01 --region us-east-1 \
  check --name readera-...sqlite3.rbe

python -m chaptera_server.ops_backup_s3 \
  --bucket PRIVATE_BUCKET --vault readera-vault-01 --region us-east-1 \
  pull --name readera-...sqlite3.rbe --directory /private/downloads

python -m chaptera_server.ops_backup_crypto unseal \
  --archive /private/downloads/readera-...sqlite3.rbe \
  --key-file /independent-secret-store/readera-recovery.key \
  --destination /private/offline-recovery/jobs.sqlite3
```

For an operator-approved non-AWS provider, add
`--endpoint https://s3.example.org`. Custom endpoints must be HTTPS with
no embedded credentials, query, path or fragment. Vendor region identifiers
such as `ru-central1` are allowed; raw HTTP is never allowed by the CLI.

Uploads use a fixed `readera-backup-v1/<vault>/<bundle>` object namespace;
they use `If-None-Match: *` on single PUT and multipart completion to
**refuse overwriting** existing recovery points. Large bundles use bounded
8-MiB multipart parts, up to 80 GiB. The client verifies every upload
through remote HEAD and a full remote GET/SHA-256 readback; ETags are never
used as SHA-256. Downloads are checked against remote size/SHA-256 and
published only to a NEW private local file. The remote metadata digest is
not a cryptographic signature; **only AES-256-GCM authentication with the
independently recovered key** proves authenticity of the restored SQLite.

No bucket, retention policy, credentials, off-host account, automatic
backup schedule or deletion job is created by merging this code. ReadEra's
API, worker and UI never call the S3 transport. To accept operational
recovery, separately recover the key from a secret store, download with a
different host/identity, unseal, verify restored books/jobs/authorization,
and record a redacted proof under
[issue #56](https://github.com/Lalalalendia/ararra/issues/56).
