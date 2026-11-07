# Increment 01: executable foundation (synthetic adapters only)

Engineering contract: `README.md` (unchanged by this increment). This increment is a
clean-room implementation; no code from any earlier bridge was copied or consulted.

**Live WeChat and live primary GrokBot integrations are NOT implemented and NOT
verified.** No perception, ASR, TTS or phone delivery is performed anywhere in this
code. Synthetic adapters move real bytes through the bridge boundaries but every
understanding claim they emit is labelled `synthetic` and comes from a fixture script.

## Build and test

Requires Python >= 3.11, standard library only, no network.

```sh
./scripts/check.sh
```

which runs:

```sh
PYTHONPATH=src python3 -m compileall -q -f src tests
PYTHONPATH=src python3 -m unittest discover -s tests -t tests -v
PYTHONPATH=src python3 -m grok_wechat_bridge directions
PYTHONPATH=src python3 -m grok_wechat_bridge selftest
```

Optional wheel build (offline): `python3 -m pip wheel --no-build-isolation --no-deps -w <outdir> .`

## Code map

| Path | Responsibility |
| --- | --- |
| `src/grok_wechat_bridge/ids.py` | String-only message IDs, precision-preserving JSON envelope parsing, deterministic event / idempotency keys |
| `src/grok_wechat_bridge/modality.py` | 4 kinds x 2 flows = 8 `Direction`s; capability, evidence, provenance, voice delivery form, route vocabularies |
| `src/grok_wechat_bridge/store.py` | SQLite (WAL, `synchronous=FULL`): events with `UNIQUE(conversation_id, source_msg_id)`, media, fetch log, associations, evidence, gaps, outbound send state |
| `src/grok_wechat_bridge/media.py` | Content-addressed media storage, size + SHA-256 checks, HMAC refs bound to media + event + conversation with TTL and revocation, logged fetches |
| `src/grok_wechat_bridge/formats.py` | Container checks: complete MP4 box tiling, video/audio track detection, image and voice container sniffing |
| `src/grok_wechat_bridge/routing.py` | Primary GrokBot by default; explicit delegation flag only on explicit prefixes |
| `src/grok_wechat_bridge/bridge.py` | Orchestrator: binding, dedupe, capture, association, capability gates, run, evidence validation, outbound planning, send state machine, resume |
| `src/grok_wechat_bridge/adapters.py` | WeChat / primary GrokBot adapter protocols; live adapters raise `NotImplementedLiveIntegration` |
| `src/grok_wechat_bridge/synthetic.py` | Synthetic WeChat (fault injection, receipt lookup) and synthetic primary GrokBot (scripted evidence) |
| `src/grok_wechat_bridge/report.py` | Per-event acceptance record; per-direction status table |
| `src/grok_wechat_bridge/fixtures.py` | Synthetic PNG / MP4 skeleton / WAV / SILK-header bytes generated in code |
| `tests/` | 74 unittest cases incl. repository hygiene scan |

## Per-direction status

"Core" = bridge-side logic in this repo, exercised by tests. "Mock" = what the synthetic
adapter stands in for. "Missing" = not present in this increment.

| Direction | Implemented (core) | Mock only | Missing |
| --- | --- | --- | --- |
| text WeChat -> GrokBot | Binding to one conversation, string-ID dedupe across restart, explicit-delegation flag, bounded association (quote / 120 s window / max 4 media) | WeChat inbound delivery; GrokBot reply text | Live WeChat receive, live GrokBot invocation, execution-agent handoff |
| text GrokBot -> WeChat | Deterministic idempotency key, durable `planned/sending/unknown/interface_accepted/rejected/failed`, receipt check before any retry, attempt cap, delivery receipt and phone confirmation as separate fields | WeChat send + receipt lookup | Live send, live receipt API, phone confirmation workflow |
| voice WeChat -> GrokBot | Audio capture + SHA-256/size, ASR/audio capability gate, same-message platform transcript fallback (recorded as gap + `wechat_platform` evidence), primary must fetch audio and return transcript/audio evidence citing that SHA-256 | Transcript text (scripted, **not ASR**) | Live audio download, real ASR / audio understanding, SILK decoding |
| voice GrokBot -> WeChat | Native voice vs audio-attachment recorded separately; native unavailable is never silently downgraded; attachment requires explicit `downgrade_confirmed`; container check | Voice bytes (synthetic WAV, **not TTS**) | Live TTS, WeChat native voice encoding, phone playback check |
| image WeChat -> GrokBot | Original bytes stored + hashed, declared size/hash check, bound ref fetch logged per agent, view-detail evidence required for that SHA-256, stale/foreign evidence rejected | View-detail evidence (scripted, **not perception**) | Live image download, real vision |
| image GrokBot -> WeChat | Image container check, hashed outbound media, channel capability gate, idempotent send | WeChat image send | Live send, phone open check |
| video WeChat -> GrokBot | Complete-MP4 check (ftyp first, moov + mdat, boxes tile file exactly), video/audio track detection, visual and audio evidence required separately, silent video recorded as `video.audio_track_absent` | Visual and audio evidence (scripted, **not perception**) | Live download, real frame / audio understanding, codec-level validation |
| video GrokBot -> WeChat | Complete-MP4 + video-track check, hashed outbound media, capability gate, idempotent send | WeChat video send | Live send, phone playback check |

`python3 -m grok_wechat_bridge directions` prints the same status from code
(`report.DIRECTION_STATUS`).

## Behaviour notes

- Unbound conversations: ignored, nothing persisted.
- Every failure path (`bridge.Gap`) records a gap row and replies to the original
  conversation with an explicit `[桥接缺口]` text; the primary's unverified answer is
  dropped, and no other/old media or task is substituted.
- A run result is rejected when its `event_id`/`agent_id` differ from the request or
  any evidence cites a SHA-256 not provided to this event.
- Image/video sent without text are held for the association window; `Bridge.tick()`
  processes them alone once the window closes.
- `perception_live_verified` in the acceptance record is false whenever any perception
  evidence is synthetic; `phone_acceptance_complete` requires owner confirmation on
  every outbound item.

## Limitations (known, not hidden)

1. No live adapters. Nothing here talks to WeChat, GrokBot, ASR, TTS or a phone.
2. Media-ref HMAC key is per process; refs do not survive restart (by design), so a
   restart mid-run re-issues refs and re-runs the primary GrokBot for `running` events.
   Outbound idempotency keys prevent duplicate sends, but the primary may run twice.
3. If a crash happens after an event is claimed but before media is stored, resume
   fails that event explicitly (`media_missing`) rather than re-downloading; the
   platform media handle is not persisted.
4. Question-then-media ordering (text first, media later) is not associated; only
   media-then-question and quote-based association are implemented.
5. Container checks are structural only (no decoding). SILK fixtures are header-only.
6. Explicit delegation is a flag passed to the primary GrokBot; the bridge does not
   call any execution agent itself.
7. Single-process, single-writer SQLite; no concurrency control beyond SQLite locks.
8. Media blobs have no retention / deletion policy yet.
9. No encryption at rest for stored media or the SQLite file (file mode 0600/0700 only).

## Independent CSO review checklist (not self-approved)

Reviewer fills each item; the author has not approved any of them.

- [ ] Clean-room: no code, config or identifiers from any legacy bridge repository.
- [ ] Tracked files contain no credentials, real WeChat identifiers, real media, logs,
      runtime data or personal machine paths (`git ls-files`, plus the hygiene test).
- [ ] `.gitignore` keeps runtime state (`*.sqlite`, `data/`, `media/`, `.env*`) untracked.
- [ ] Message IDs: no code path converts IDs to int/float (`ids.py`, `bridge.ingest`).
- [ ] Only the bound conversation is processed; unbound input leaves no trace in storage.
- [ ] Media refs: HMAC-SHA256, constant-time compare, TTL, revocation, bound to media,
      event and conversation; cross-event use requires a recorded association.
- [ ] Every agent fetch re-verifies size and SHA-256 and is logged with the agent ID.
- [ ] Inbound/outbound media are format-checked before storage/sending; incomplete MP4
      is rejected.
- [ ] No path from the bridge sends media to any agent other than the primary GrokBot.
- [ ] Delegation requires explicit user wording; review the regex in `routing.py`.
- [ ] Missing media / ASR / vision / video-audio capability fails explicitly; no
      substitution of earlier tasks or media; mismatched run results are discarded.
- [ ] Send state: status UNKNOWN never triggers a blind resend; INDETERMINATE receipt
      leaves the item UNKNOWN; attempts are capped.
- [ ] Interface acceptance, platform delivery receipt and phone confirmation are separate
      and receipts cannot be attached to unaccepted items.
- [ ] Voice output: native vs attachment recorded; attachment downgrade needs explicit
      confirmation; no silent downgrade.
- [ ] Synthetic evidence is labelled `synthetic` and never sets `perception_live_verified`.
- [ ] Gap texts and evidence details treat external/model content as data (no
      instruction execution from transcripts, OCR text or adapter output).
- [ ] Storage permissions (0700 dirs / 0600 files) and the lack of encryption at rest
      are acceptable for the intended deployment, or tracked as follow-up.
- [ ] Limitations above are acceptable for a first increment and tracked.
- [ ] `./scripts/check.sh` reproduces on a clean checkout with no network.
