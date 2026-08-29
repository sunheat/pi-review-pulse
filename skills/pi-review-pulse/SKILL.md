---
name: pi-review-pulse
description: Run a Codex-first, PR-scoped review remediation loop with stable snapshots, frozen Codex batches, exact thread resolution, one aggregate publication, and safe completion-relative one-shot self-reschedules inside pi coding agent. Use for GitHub pull requests reviewed by Codex; generic reviewers and other forges are unsupported.
---

# Pi Review Pulse

Use the Codex-first default path for requests such as:

```text
Automatically fix this pull request's Codex review issues until no new issues appear.
```

The default path is the public product surface. It identifies the current PR
when the checkout identifies one, asks one short question only when the target
is ambiguous, and runs one recoverable wake at a time. Its default policy is
aggressively autonomous and unbounded: it may repair PR-scoped code and tests,
retry recoverable failures, publish the aggregate batch, resolve exact target
threads, and register one one-shot successor wake until a Codex-specific stop
condition or an explicit safety boundary is reached. It does not require a run contract,
observation JSON, doctor, pilot preflight, immutable installation, runner
identity, authority digest, owner token, or renewable lease.

Version `0.4.0` has a real black-box pilot failure. It is not a publishable
final recurring release. The failure evidence is preserved in the repository:
the old heartbeat activated too early, used fixed-cadence overlap, allowed a
300-second lease to expire during a long wake, double-planned one host wake,
and continued after `PAUSE_BLOCKED`. Do not describe `0.4.0` as production
ready or use its old scheduled-task protocol as the default.

Version `0.6.0` is the Codex-first default automation-policy candidate. Its
real scheduled-task and live GitHub integration remains unverified until an
independent forward test completes; do not describe that integration as proven
before then.

This pi port binds scheduling to the `pi-schedule-prompt` npm package and its
`schedule_prompt` tool. That one-shot integration has not been run or
production-validated; do not describe pi scheduling as proven before an
independent forward test completes.

## Default control surface

Use the single main entry point:

```text
skills/pi-review-pulse/scripts/pulse.py
```

Its small subcommands are `begin-wake`, `snapshot`, `freeze`, `record`,
`resolve`, `retry`, `trigger-result`, `publication-result`,
`configure-policy`, and `complete-wake`.
`snapshot` returns an agent-facing normalized object with top-level
`head_oid`, PR state, targeted and non-target threads, Codex review activity,
approval evidence, review-epoch state, and head-bracketing server evidence.
Do not manually translate `fetch_pr_state.py` output into another observation
schema.

The CLI resolves `OWNER/REPO` and the PR number before calculating the default
checkpoint path. From a checkout whose current branch identifies one PR, the
default commands do not need `--repo` or `--pr`; explicit values still take
priority. A missing or ambiguous current PR stops with an actionable error
instead of guessing. The checkpoint remains under the target repository's Git
common directory. After initialization, later commands use the checkpoint's
bound repository and PR and reject any explicit target that differs.

## Default automation policy

The first user request is translated into a normalized policy and persisted in
the PR-scoped checkpoint. Unless the request says otherwise, the policy is:

- `profile=autonomous`, `execution_mode=unattended`;
- `cadence_seconds=600`;
- `max_wakes=null`, `deadline_at=null`, and `retry_wake_limit=null` (no
  artificial wake, time, or retry budget);
- `validation_failure=repair` and `allow_test_changes=true`;
- `publication=auto`, `thread_resolution=auto`, and `review_trigger=auto`;
- `inline_retry_limit=3`, `no_progress_limit=3`, and
  `notifications=blockers-and-terminal`.

The policy is a control contract, not a request to bypass the hard invariants
below. Codex-only targeting, current-head proof, frozen exact thread IDs,
explicit-path staging, one aggregate publication per batch, no force-push or
merge, and pause-on-uncertainty always remain in force. `null` limits are
deliberately unbounded; if a user supplies a wake count or deadline, it is
persisted and enforced at the next wake.

`pulse.py` enforces lifecycle-level limits and mutation gates; the executing
agent applies `execution_mode`, `allow_test_changes`, `inline_retry_limit`, and
`notifications` while choosing local edits, validation, and reporting.

Prompt instructions override these defaults. The host agent should convert them
to the corresponding JSON fields before the initial `begin-wake`, for example:

```json
{"max_wakes": 5, "deadline_at": "2026-08-27T10:00:00+10:00"}
```

`--policy-json` accepts the same object on the initial wake. Outside an active
wake, `configure-policy --policy-json '{...}'` updates the persisted policy;
policy changes are never made halfway through a frozen batch. Useful explicit
profiles are `supervised` (confirm publication, resolution, and triggers) and
`observe-only` (no PR mutations). A prompt such as “keep working unattended,
update stale PR-scoped tests when the implementation is correct, and retry
transient failures until the review is clean” selects the default autonomous
profile and needs no extra flags.

In pi, there is no concurrent background timer to pause. The unchanged CLI
still accepts `--pause-confirmed`; pass it as the no-op acknowledgement that no
pi timer needs pausing, not as a claim that a scheduler pause call occurred.
Pass `--schedule-reanchored` to `complete-wake` only after `schedule_prompt add`
returns a real `jobId`. These flags do not pause or schedule a job themselves;
the flag value is never evidence that `schedule_prompt add` succeeded.

### Default CLI/host sequence

The executing pi agent owns the `schedule_prompt` operations; `pulse.py` only
records their confirmed results. Use one new opaque `wake_id` for each pi wake
and reuse that exact value for every `pulse.py` command in the wake. Do not
reuse it for the next scheduled wake. Pi has no concurrent background timer, so
there is no scheduler pause step.

```text
PULSE = "python skills/pi-review-pulse/scripts/pulse.py"
TARGET = "--repository-path PR_CHECKOUT --repo OWNER/REPO --pr NUMBER"

# Use the same WAKE_ID for begin, snapshot, freeze, record, resolve, retry,
# publication-result, trigger-result, and complete-wake.
# --pause-confirmed is the unchanged CLI's no-op acknowledgement: pi has no
# concurrent timer to pause, so no pause tool call is made.
PULSE TARGET --wake-id WAKE_ID begin-wake \
  --pause-confirmed
snapshot = PULSE TARGET --wake-id WAKE_ID snapshot

if snapshot.decision.next_action == RUN_BATCH:
    PULSE TARGET --wake-id WAKE_ID freeze
    for each frozen thread:
        PULSE TARGET --wake-id WAKE_ID record --thread-id ID \
          --classification fix-now|no-fix|defer|ambiguous
        # Apply and focused-validate a fix when classification is fix-now.
        # In autonomous mode, repair stale PR-scoped tests or implementation
        # defects when the behavior contract is correct. If a recoverable
        # failure remains, persist it and end this wake:
        PULSE TARGET --wake-id WAKE_ID retry \
          --reason-code validation_retry --signature FAILURE_SIGNATURE
        # Otherwise, after the focused check passes:
        PULSE TARGET --wake-id WAKE_ID resolve --thread-id ID
    # Only after every exact resolution succeeds in this wake:
    PULSE TARGET --wake-id WAKE_ID publication-result --status succeeded

if snapshot.decision.next_action == REQUEST_REVIEW:
    # Perform one authorized, same-head bracketed trigger and write its evidence.
    PULSE TARGET --wake-id WAKE_ID trigger-result --evidence trigger.json

if snapshot.decision.next_action is PAUSE_* or STOP_*:
    # Do not call schedule_prompt add. End this wake; the persisted pulse
    # result remains PAUSED/terminal and no successor job is registered.
    end this wake

# WAIT_REVIEW, WAIT_RETRY, or a successfully recorded same-head REQUEST_REVIEW
# may re-anchor. WAIT_RETRY resumes the same frozen batch on the next wake.
# Choose COMPLETION_NOW once and use it for both actions.
COMPLETION_NOW = current UTC time
NEXT_NOT_BEFORE = COMPLETION_NOW + cadence_seconds
reanchor = schedule_prompt add(
    type="once",
    schedule="+<cadence_seconds>s",
    prompt=PI_WAKE_PROMPT,
)
JOB_ID = reanchor.jobId
PULSE TARGET --wake-id WAKE_ID --now COMPLETION_NOW complete-wake \
  --schedule-reanchored

# If schedule_prompt add fails or does not return a jobId, do not pass
# --schedule-reanchored:
PULSE TARGET --wake-id WAKE_ID --now COMPLETION_NOW complete-wake
# This persists PAUSE_BLOCKED / scheduled_task_reanchor_unavailable.
# Never replace this one-shot add with a fixed interval.
```

The agent must inspect the actual `schedule_prompt add` response and require a
non-empty returned `jobId` before passing `--schedule-reanchored`. A boolean,
model statement, or successful-looking command line is not a scheduler-tool
result. For the initial user turn, run the current pi session as wake 1; do
not create a separate recurring task or schedule a job before that wake. The
CLI options are also visible in `pulse.py begin-wake --help` and
`pulse.py complete-wake --help`.

The default path reuses the core state evaluator, head-bracketed GraphQL
retrieval, atomic Git-common-directory checkpoint, frozen-batch transitions,
and exact GraphQL resolver. It does not duplicate those implementations and
does not import the hardened authority machinery.

## One host wake, one plan

Treat the initial user turn as wake 1. When creating or updating the one
heartbeat for the same task, leave it `PAUSED`; never activate it before wake
1 starts.

At the start of every scheduled wake, the first scheduler operation must pause
that heartbeat. Continue only after the host confirms the pause. If pause
confirmation is unavailable or fails, persist `PAUSE_BLOCKED`, keep the
heartbeat paused, and end the turn without snapshot, freeze, resolve, commit,
push, trigger, or another plan.

Persist an opaque `wake_id` and at least these fields in the default checkpoint:

- `active_wake_id`
- `wake_phase`
- `wake_started_at`
- `wake_completed_at`
- `next_not_before`
- `scheduled_task_disposition`
- `wake_count`

One host wake may successfully begin and plan once. Repeating `plan` or
`snapshot` with the same `wake_id` returns the prior result or rejects without
incrementing `wake_count`. Lease renewal, snapshot refresh, completion, and
recovery inspection are not new wakes. A stale or incomplete marker produces
`PAUSE_RECOVERY`; it never auto-takes over the marker.

Run at most one stable snapshot/decision and one frozen batch per wake. While a
configured Codex identity has PR-level `EYES`, return `WAIT_REVIEW` and do not
freeze partial threads. After `EYES` disappears, freeze all targeted unresolved
Codex root-author thread IDs from the stable current head. Process each thread
as `fix-now`, `no-fix`, `defer`, or `ambiguous`; persist its outcome before
resolving that exact GraphQL node. Ambiguous/conflicting evidence pauses.

In the autonomous profile, a failed focused check is a repair signal, not an
automatic stop. If the behavior contract is correct, update a stale
PR-scoped test or fixture when `allow_test_changes` is true, rerun the focused
check, and continue. For a transient external or environment failure, use
`retry` to persist the failure signature and end the wake with `WAIT_RETRY`;
the next completion-relative wake resumes the same frozen batch. A repeated
unchanged signature reaches `no_progress_limit` and pauses. If the policy sets
`validation_failure=pause` or disallows test changes, stop at that boundary.

After all exact resolutions, run aggregate validation and publish at most one
aggregate commit and one push. A no-fix-only batch does not create an empty
commit. Do not process review artifacts created by that push until a later
wake.

The following are default-path safety rules, not optional hardened ceremony:

- For `fix-now`, focused validation must pass before resolving that exact
  thread. Repair the implementation or a stale PR-scoped test first when the
  autonomous policy permits; otherwise leave the thread unresolved and pause.
- Before creating the aggregate commit, re-read the authoritative remote PR
  head and require it to equal the frozen head OID.
- Stage only explicit intended paths and inspect the staged diff; never stage
  the whole worktree.
- Immediately before the single push, re-read the remote PR head again and
  require it still equals the frozen head OID. If the branch advanced, stop
  and do not overwrite it.
- After pushing, verify that the remote PR head equals the new pushed commit;
  a mismatch is a publication failure. Never force-push or change the PR base.

## Completion-relative scheduling

Only a final `WAIT_REVIEW`, `WAIT_RETRY`, or a successful `REQUEST_REVIEW` whose
trigger evidence proves the same head before and after the trigger, may rearm
the next wake. Set:

```text
next_not_before = wake_completed_at + cadence_seconds
```

Never rely on pausing and reactivating a fixed RRULE to reset its clock. The
host must re-anchor the next run to `next_not_before`. If the host cannot prove
that completion-relative schedule, keep the disposition `PAUSED` and report
`PAUSE_BLOCKED / scheduled_task_reanchor_unavailable`.

`STOP_*`, every `PAUSE_*`, recovery, closed/merged, expired, publication
failure, lease loss, and unknown results remain `PAUSED` and do not schedule a
next wake. `STOP_POLICY_LIMIT` is the explicit result for a configured wake,
deadline, or retry bound. Persisted `terminal` or `closed` phases are absorbing
across later wake IDs too: `begin-wake` rejects them without incrementing
`wake_count`. Reopening requires an explicit new user instruction. Pause is
absorbing for the current turn: persist the reason and evidence, stop
immediately, and never clear its latch in the same turn. A
non-empty string called `recovery_authorization_id` is not proof of user or
external authorization. The default path has no automatic latch-clearing
operation; recovery starts only in a new user turn or from a separately
verifiable external authority.

## Scheduled-task handoff

The Python entry point does not call a private Codex automation API. The
executing pi agent owns the following one-shot self-rescheduling handoff:

1. Treat the user's initial task as wake 1.
2. Pi has no concurrent background timer to pause. Do not create a recurring
   task or perform a pause operation before `begin-wake`; invoke the unchanged
   CLI with `--pause-confirmed` only as the no-op acknowledgement that no pi
   timer needs pausing.
3. Run this wake's snapshot, frozen batch, repair/retry, outcome/resolve, and
   aggregate publication work.
4. For `WAIT_REVIEW`, `WAIT_RETRY`, or successful same-head `REQUEST_REVIEW`, choose one
   completion timestamp and compute
   `next_not_before = wake_completed_at + cadence_seconds` without calling
   `complete-wake` yet.
5. Call `schedule_prompt add` with `type=once` and
   `schedule=+<cadence_seconds>s`, then inspect the actual response.
6. Require the returned non-empty `jobId`; this is the only proof that the
   one-shot successor job was registered. Do not use a fixed interval.
7. After `schedule_prompt add` succeeds, call `complete-wake` exactly once with
   the same completion timestamp and `--schedule-reanchored`.
8. If `schedule_prompt add` fails or returns no `jobId`, call `complete-wake`
   exactly once without `--schedule-reanchored`; this persists the re-anchor
   blocker and keeps the disposition `PAUSED`.
9. On `PAUSE_*` or `STOP_*`, do not call `schedule_prompt add`; end the wake
   without registering a next job. On any other tool failure or unproven
   success, do the same. Never treat a model assertion, boolean argument, or
   natural-language claim as proof that scheduling succeeded.
10. `pi-schedule-prompt` fires only while a pi session is open in the target
    directory; when no such session is open, nothing is queued. This is why
    every eligible wake self-registers one `type=once` successor instead of
    relying on a fixed interval. This handoff is not production-validated yet.

## Review termination

Stop only for a Codex-specific result, never as a claim of global merge
readiness:

1. an `APPROVED` review by a configured Codex identity whose
   `review.commit.oid == headRefOid` and no targeted threads;
2. a newly proven current-head Codex `THUMBS_UP` reaction epoch and no targeted
   threads; or
3. Codex `EYES` was observed on this head, then disappeared in a newer stable
   snapshot, and targeted unresolved threads are zero.

Cold-start or first-observation historical reactions remain ambiguous.
`EYES` is review activity, never approval. A head with targeted threads is
always processed even if an earlier epoch saw `EYES`. After one safely
bracketed `@codex review` trigger for a head, a following empty wake pauses
with evidence instead of triggering again.

## Authorized default scope

The standard short request authorizes autonomous PR-scoped implementation and
test edits, repair of stale PR-scoped expectations, recoverable retries,
targeted Codex thread exact resolution including recorded no-fix outcomes, one
aggregate commit and push per batch, one trigger per head, and creation/update/
pause/reanchor of one same-task heartbeat. It remains active across scheduled
wakes until a Codex-specific terminal result or a hard blocker. A prompt can
narrow this scope by selecting `supervised`, `observe-only`, explicit limits,
`allow_test_changes=false`, or confirmation policies. It never authorizes issue
creation, merge, auto-merge, base changes, force-pushes, generic reviewers,
non-target threads, or unrelated changes.

Host permissions are a separate boundary: unattended operation requires the
host task/thread to have network access, full workspace access, and a
non-interactive approval policy. The prompt can narrow behavior, but cannot
grant capabilities that the host has not granted.

This repository phase implements and tests the control logic only. Do not
create a real scheduled task, mutate live GitHub, install the skill, commit,
or push while developing or validating this change.

## Deferred hardened mode

The source repository's immutable installation, pilot preflight, run contract,
authority digest, renewable lease, doctor/plan/complete, and recovery-latch
machinery is intentionally not vendored into this pi skill. It is not a
prerequisite for the default path, and the 0.4.0 black-box pilot failure means
it is not currently a publishable final recurring mode.

Codex-only targeting, stable head snapshots, frozen batches, exact resolution,
one aggregate publication, and unrelated-work protection apply to this pi
default path. Generic reviewers, multi-forge support, live long-term unattended
integration evidence, and `gh-address-comments` integration remain deferred.
