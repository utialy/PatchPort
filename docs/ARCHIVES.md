# Archived results and evidence

Only archive readers are implemented. There is no archive writer, whole-flow cleanup command, or automatic migration of existing flows.

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

The planned writer must preserve exact input, answer, diff, and test-log evidence before deleting duplicate copies. Private queues, locks, and a flow marker remain in place. Applied, incomplete, or damaged recovery backups block flow cleanup. A future command needs a read-only preview, plan hash, flow-lifetime lock, durable progress journal, and explicit continuation after interruption. These are design requirements, not current commands.
