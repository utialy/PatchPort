# Archived results and evidence

Flow-lifetime locking, read-only cleanup planning, archive creation, and explicit archive continuation are implemented. Whole-flow deletion and automatic migration are not available.

## Archive without deleting originals

```sh
python bridge.py role-manage archive-flow --id FLOW_ID --days 30
python bridge.py role-manage archive-flow --id FLOW_ID --days 30 --plan-hash SHA256 --apply
python bridge.py role-manage archive-flow --id FLOW_ID
python bridge.py role-manage archive-flow --id FLOW_ID --evidence-source flow_metadata --evidence-path test-0.log
```

The first command returns the same read-only plan as `prune-flow`. Creation requires an eligible plan and its unchanged hash. It checks terminal role identity, retention, recovery backups, file types, configuration, and queue snapshots. Unknown files, active work, unsupported legacy flows, SQLite sidecars/WAL, and nonpositive projected cleanup savings block the candidate. Archiving itself consumes additional space and deletes nothing.

The writer holds the flow, parent runner/promotion, and existing role-state runner/promotion locks. It preserves the original result.json, workspaces, and queue bytes. Evidence is copied byte-for-byte, deduplicated by SHA256, flushed, rechecked, and published under archive/. The ARCHIVE_ONLY cleanup journal records PREPARING, ARCHIVE_FAILED, or ARCHIVE_READY. On POSIX, relevant directories are also fsynced; portable Python does not provide the same directory-flush guarantee on Windows.

After creation, `archive-flow` validates all referenced blobs and returns the historical snapshot. Evidence queries return UTF-8 or base64. The normal role-review/result/overview commands continue to read retained live artifacts. Usage comes only from the owning queues, never from a second archive total.

## Continue an interrupted archive

Read `operation` and `plan_hash` from the flow's cleanup.json, then inspect before applying:

```sh
python bridge.py role-manage archive-flow --id FLOW_ID --continue OPERATION
python bridge.py role-manage archive-flow --id FLOW_ID --continue OPERATION --plan-hash ORIGINAL_SHA256 --apply
```

Continuation uses the original retention period; it rejects `--days`. Journals with resume_schema=1 retain the original inventory, configuration, queue fingerprints, records, and plan hash. The read-only inspection and locked execution recheck them. Missing archive files are created; existing evidence must already match. If publication finished but its final journal update failed, the archive is verified before marking it ready. Repeating an already completed operation is read-only.

Changed sources or queues, corrupt partial files, unexpected entries, conflicting archive/staging directories, and legacy journals without a persisted plan are rejected. Even unrelated changes to the shared parent queue conservatively invalidate a pending operation. Failed output is retained for inspection; there is no automatic retry, overwrite, abandon, or deletion. Provider tasks are never rerun by continuation.

Incomplete or corrupt archives block role promotion/recovery/prune. A fully verified ARCHIVE_READY snapshot permits normal live role management again; the snapshot remains historical and is not overwritten. `prune-flow --apply` remains unsupported.

## Readers for compacted-layout fixtures

The following readers also understand the ARCHIVED marker layout. The current writer does not replace result.json with that marker or compact workspaces.

```sh
python bridge.py role-review --id FLOW_ID --role reviewer
python bridge.py role-review --id FLOW_ID --role reviewer --evidence-path __bridge_review__/test-0.log
python bridge.py role-review --id FLOW_ID --role reviewer --evidence-source flow_metadata --evidence-path test-0.log
python bridge.py develop-review --id FLOW_ID --result
```

Evidence paths are logical paths in the manifest, not arbitrary disk paths. Sources are original_input, review_input, developer_workspace, reviewer_workspace, and flow_metadata. The selected role's workspace is the default. Text is returned unchanged as UTF-8; other bytes are returned as base64 with an encoding label.

## Format

An archive lives under .role-flows/FLOW_ID/archive. manifest.json contains exactly schema=1, flow, entries, records_sha256, and archive_digest. Each file entry has source, path, kind=file, sha256, size, and mtime_ns. Source/path pairs are unique. Directory entries are not supported yet.

The manifest digest excludes its own archive_digest field. It hashes UTF-8 JSON with sorted keys, separators=(',', ':'), ensure_ascii=False, and allow_nan=False. records_sha256 hashes the original records.json bytes. Blobs are stored under blobs/SHA256 and may be shared within one archive by identical file contents.

records.json requires schema=1, flow, queue_mode (PRIVATE or SHARED_PARENT), original_phase, roles, and checks. Optional fields are error and original_changed_since_snapshot. Other fields are rejected. Each submitted role has task, terminal state, answer, and changes; an unsubmitted role is null. Answer text/status, change hashes/permissions, and check argv/exit_code/log are validated. Check logs must reference flow_metadata entries. REVIEW_DONE requires both roles to be DONE.

The flow result.json marker requires id, state=ARCHIVED, artifact_layout=FLOW_ARCHIVE_V1, original_phase, queue_mode, archive_digest, and cleanup_operation. cleanup.json must match schema=1, flow, operation, and archive_digest. Supported journal states are ARCHIVE_READY, DELETING, DELETE_FAILED, and COMPACTED.

Public archive queries validate the marker, journal, records, and all referenced blobs. Evidence reads verify their selected blob again. Digests detect inconsistent bytes; they are not signatures or proof that a historical execution was truthful. Concurrent file replacement is outside the read-only observation guarantee.

## Query semantics

Archive output identifies its artifact source/state and sets current_files_checked=false. Current original/proposal matches and writable eligibility are not inferred from historical records. role-review returns recorded_allowed separately from current allowed=null.

overview displays historical records separately and reads usage from the existing owning queue only. Archive snapshots are not added to totals or used to replace a missing private DB. Task identity/state conflicts and archive corruption are reported while readable queue usage remains available.

Archived result views return usage=null with QUERY_OWNING_QUEUE. Use overview for actual recorded usage. Existing promotion, recovery, and workspace-prune operations reject archived flows with FLOW_ARCHIVED.

## Planned cleanup

Future whole-flow deletion must preserve exact evidence, private queue history, locks, and recoverability before removing selected copies. The existing archive-only continuation is not permission to delete. Applied, incomplete, or damaged recovery backups still block cleanup candidates.
