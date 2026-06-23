# Dot Compliance — GTM Engineer Take-Home

**The finding that drives everything here:** Dot Compliance's own webinar export proves that
self-reported intent is noise. Across 83 registrants, exactly **one** is a real buying signal —
and it sits on the single most corrupted row in the file. So the build splits cleanly:

- **Option 1** is the hygiene/routing layer that gets clean leads into Salesforce.
- **Option 2** is the engine that actually generates pipeline — on *behavioral* signal from a
  separate source, because that's where real intent lives, not in what people tell a form.

One dataset exposed the problem; the two builds are the fix. The full write-up — thesis, both
builds, and the Tooling & Cost assessment — is in **[`deliverable.md`](deliverable.md)**. Start there.

## What's in this repo

| File | What it is |
|---|---|
| [`deliverable.md`](deliverable.md) | The submission: thesis → Option 1 (ingestion) → Option 2 (intent loops) → Tooling & Cost. The Mermaid diagrams render inline on GitHub. |
| [`option1_ingest.py`](option1_ingest.py) | The runnable Option 1 pipeline — pure Python standard library, no dependencies. The decision-logic layer. |
| [`option1_schema.json`](option1_schema.json) | The Salesforce upsert JSON schema + field map (JSON Schema Draft 2020-12). |
| [`option1_output.csv`](option1_output.csv) | Pre-generated output — one annotated row per registrant, every routing decision auditable. |
| [`option1_sample_payloads.json`](option1_sample_payloads.json) | Representative upsert payloads, one per routing branch; all validate against the schema. |

## Running it

Pure standard library — Python 3, nothing to install:

```bash
python3 option1_ingest.py
```

The committed `option1_output.csv` and `option1_sample_payloads.json` are the pre-generated
outputs, so you can read the results without running anything.

To **reproduce** them, place the provided webinar attendee export next to the script as
`attendee-data.csv` (or in a `source/` subfolder) and re-run — the script regenerates both files.
It's idempotent: two runs produce byte-identical output.
