# CLI contract

Commands emit one JSON result on stdout; diagnostics/dependency progress go to stderr.
Exit 0 is success, 1 is an error (including completed ingestion with failed paths),
130 is interruption. Global options precede the command:

```text
scripts/snytro --collection contracts index "/selected/contracts"
scripts/snytro --collection contracts status <job-id>
scripts/snytro --collection contracts list --limit 100 --offset 0
scripts/snytro --collection contracts search "termination notice" --stage taglines
scripts/snytro --collection contracts summaries --document-ids <id1> <id2>
scripts/snytro --collection contracts structured --document-ids <id1> --fields total currency
scripts/snytro --collection contracts search "termination notice" --stage chunks --semantic --document-ids <id1> <id2>
scripts/snytro --collection contracts search "termination notice" --stage pages --document-ids <id1>
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

Catalog stages are keyword retrieval; `chunks --semantic` fuses keyword and vector
results using reciprocal rank fusion. KNN filters apply to collection and document
IDs before top-k. Scores across retrieval surfaces are not interchangeable.
Source evidence contains filename/path, content checksum and locator. Readiness
reports analysis and embedding state separately. Extractive previews do not imply
completed semantic compression.

`fetch` returns stored extraction, not a reread of a potentially changed original.
It reports the indexed checksum; reindex explicitly if current disk content is required.
PDFs keep physical page numbers; DOCX/text use document units; XLSX keeps formulas
and labels sheet/row ranges. Scanned PDFs require external OCR. Legacy .doc/.xls,
RTF, images and slide decks are not supported in v0.1.

`structured` returns saved fields with source unit IDs. `--fields` supports dotted
object paths; arrays remain available in the full fields object. No arbitrary SQL
is exposed. Perform scoped arithmetic/aggregation on returned numeric fields and
fetch originals to verify important amounts. Document extraction rows are additional
evidence and never replace the original hierarchy.
