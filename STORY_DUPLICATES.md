# Conservative story duplicate prevention

Idea generation derives canonical keys lazily from all topic lists in the selected
channel's existing memory. Memory stays topic-to-string-list JSON. Entries are
accepted proposals, not evidence of generation or publication. No history migration,
queue change, new retry loop, or cross-channel generation lookup is performed.

Known event aliases match only when remaining words are generic framing or the
registered event year. Substantive qualifiers and conflicting years retain separate
identities. Unknown titles receive typography normalization and narrow introductory
framing removal; uncertain paraphrases intentionally remain allowed. This is not
universal entity recognition. Extend event rules only with false-positive tests.

Content-policy validation runs before acceptance. If every proposal is rejected,
the existing ContentGuardrailError path handles the failure and existing callers
control retries. All accepted proposals are still saved; up to three are returned.

Cross-channel visibility is explicit and read-only (use actual restored memory paths):

```powershell
python -B -m engine.generators.story_identity --channel-memory minute_mysteries=path/to/mystery-memory.json --channel-memory space_science=path/to/space-memory.json --channel-memory hidden_history=path/to/history-memory.json
```

The report emits canonical_story_key, channel proposal strings, and unknown publication
status. Missing inputs fail rather than silently implying no overlap. It neither blocks
cross-channel reuse nor authenticates, restores artifacts, or writes files. Checkpoint
state contains idea_memory; export that mapping separately before using this command.
