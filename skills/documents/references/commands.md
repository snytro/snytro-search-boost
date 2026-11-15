# CLI contract

Commands emit one JSON result on stdout; diagnostics/dependency progress go to stderr.
Exit 0 is success, 1 is an error (including completed ingestion with failed paths),
130 is interruption. Global options precede the command:

```text
scripts/snytro --collection contracts index "/selected/contracts"
scripts/snytro --collection contracts index --full --rich "/selected/contracts"
scripts/snytro --collection contracts status <job-id>
scripts/snytro --collection contracts list --limit 100 --offset 0
scripts/snytro --collection contracts search "termination notice" --stage taglines
scripts/snytro --collection contracts summaries --document-ids <id1> <id2>
scripts/snytro --collection contracts structured --document-ids <id1> --fields total currency
scripts/snytro --collection contracts search "termination notice" --stage chunks --semantic --document-ids <id1> <id2>
scripts/snytro --collection contracts search "termination notice" --stage pages --document-ids <id1>
scripts/snytro --collection contracts search "ABC-123" --stage chunks --exact --document-ids <id1>
scripts/snytro --collection contracts fetch <id1> --unit 3 --offset 0 --max-chars 12000
scripts/snytro --collection contracts save-analysis <id1> --input "/temporary/analysis.json"
scripts/snytro --collection contracts save-extraction <id1> --input "/temporary/extraction.json"
scripts/snytro --collection contracts remove <id1> --confirm
```

`index --wait` waits in the foreground (use for small fixtures); normal ingestion is
detached. `--no-recursive` restricts selected folders to immediate files. `resume`
reuses saved paths/settings and reprocesses only necessary content/embedding work.
`--document-ids` with no values intentionally matches nothing. Never omit an empty
scope just to obtain results. `list` is paginated; continue while full pages are returned.

All four stages support `--dense-only` or `--semantic` (keyword/vector fusion).
Dense ranking uses primary and context vectors and the configured source authority.
`--exact` does literal lookup; at the chunks stage it scans complete original
units, including matches across chunk boundaries. Fetch the unit it returns.
Scope, route, model and source eligibility filter candidates before ranking.
All returned rows are candidates. The invoking agent must judge relevance against
the user's requirements and exclusions; the CLI runs no internal LLM relevance
filter. Use [relevance.md](relevance.md) to accept or reject evidence before answering.
Source evidence contains filename/path, content checksum and locator. Readiness
reports analysis and embedding state separately. Extractive previews do not imply
completed semantic compression.

`fetch` returns stored extraction, not a reread of a potentially changed original.
It reports the indexed checksum; reindex explicitly if current disk content is required.
PDFs keep physical page numbers; `--rich` can produce DOCX rendition pages and
text line ranges. XLSX retains formulas and sheet/row ranges. Slides, diagrams
and image OCR have their own locators. Scanned PDFs require external OCR tools.
Legacy .doc/.xls and RTF are not supported.

`verify --check-originals` checks profile inventory, readiness and stored
checksums. See [profiles.md](profiles.md) for routes, visibility and handover.

`structured` returns saved fields with source unit IDs. `--fields` supports dotted
object paths; arrays remain available in the full fields object. No arbitrary SQL
is exposed. Perform scoped arithmetic/aggregation on returned numeric fields and
fetch originals to verify important amounts. Document extraction rows are additional
evidence and never replace the original hierarchy.
