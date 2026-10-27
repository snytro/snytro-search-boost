# Snytro Search Boost

Give **Codex and Claude Code searchable document memory** with a reusable skill
and local CLI. **Set up → Index → Ask.** Choose SQLite (default) or PostgreSQL,
and optional CPU embeddings. No MCP server, Ollama, preinstalled Python,
desktop file-manager app, or separate LLM API key is required.

Developed by **[Snytro](https://snytro.com)**. This is an early `0.1.2` release
distributed from GitHub, not an approved listing in the public plugin directories.

## Install in Codex desktop

For installation without a terminal, use the repository marketplace:

1. Open [GitHub Releases](https://github.com/snytro/snytro-search-boost/releases)
   and download the latest **snytro-search-boost-vVERSION.zip** asset.
2. Extract the ZIP. Use the inner `snytro-search-boost` folder containing
   `plugin.json`, `skills/`, and the hidden `.agents/` directory.
3. Open that folder as a local project in Codex and start a local chat. Trust the
   project when prompted. Restart the desktop app if its marketplace is not shown.
4. Open **Plugins**, select the **Snytro** marketplace/source, open **Snytro Search
   Boost**, and select the install control (a plus button or **Install**, depending
   on the app version). The project supplies `.agents/plugins/marketplace.json`.
5. Start a new local chat and ask:

   > Use Snytro Search Boost to set up a collection and index this selected folder: /path/to/my/documents.

This route discovers the marketplace through the extracted project. For a
marketplace registered independently of that project, use the one-time registration
command in the Codex CLI section below, then install through the same desktop UI.
Snytro will not appear by searching only the public directory; select its source.

These steps follow OpenAI's documented
[repository marketplace setup](https://developers.openai.com/plugins/build/plugins)
and [desktop plugin browser](https://learn.chatgpt.com/docs/plugins).

## Install in Claude desktop

Add the marketplace through the Claude app:

1. Open **Customize** in the sidebar, then **Plugins**. Some app versions place
   the plugin page under **Settings → Plugins**.
2. Select **Add → Add marketplace → Add from a repository**.
3. Paste `https://github.com/snytro/snytro-search-boost` and select **Sync** or the
   add control. Use the repository URL without a `.git` suffix.
4. Open **Snytro Search Boost** from the Snytro marketplace and select **Add** or
   **Install**. Your plan and organization must permit custom plugins.
5. Open the **Code** tab and start a **Local** session with the document folder you
   intend to work on. Use the same Claude account. Account plugins sync into
   supported Claude Code versions when a new session starts.
6. Type `/` or select **+ → Slash commands** and choose the plugin's **documents**
   skill, or type `/snytro-search-boost:documents`. Give it the selected paths.

In the Code tab's local plugin browser, **+ → Plugins → Add plugin** also lets you
install from configured marketplaces; **Manage plugins** controls enable/disable.

Use a **Local Code session** for this package's host filesystem, subprocesses, and
persistent Snytro index. Installing its skill in the ordinary Chat tab does not
provide access to your machine's CLI. Cowork may execute in a cloud sandbox or
isolated VM; this release has not validated persistent indexing there. No MCP
desktop extension or connector configuration is needed for the Local Code route.

See [Claude's desktop marketplace instructions](https://support.claude.com/en/articles/13837440-use-plugins-in-claude),
[Code tab plugins and local sessions](https://code.claude.com/docs/en/desktop),
and [Cowork execution environments](https://support.claude.com/en/articles/14479288-claude-cowork-architecture-overview).

## Install with Codex CLI

With a current Codex CLI that supports `codex plugin`:

```bash
codex plugin marketplace add snytro/snytro-search-boost
codex plugin add snytro-search-boost@snytro
```

Start a new local Codex session. In the desktop app, the Snytro marketplace is
also available in Plugins after adding it; restart the app if needed. Select and
install **Snytro Search Boost** there if you did not install it with the CLI.
Invoke the plugin's **documents** skill or ask:

> Use Snytro Search Boost to set up a collection and index this selected folder.

If `codex plugin` is unavailable, update your Codex CLI using its official
installation method, or use the manual skill installation below.
See [Codex plugin packaging and marketplaces](https://developers.openai.com/plugins/build/plugins).

## Install with Claude Code CLI

Inside a current Claude Code session:

```text
/plugin install snytro-search-boost --marketplace snytro/snytro-search-boost
```

Or from your terminal:

```bash
claude plugin marketplace add snytro/snytro-search-boost
claude plugin install snytro-search-boost@snytro
```

Start a new session, then run:

```text
/snytro-search-boost:documents
```

The single in-session install command requires Claude Code 2.1.275 or later.
See [Claude Code plugin distribution](https://code.claude.com/docs/en/plugins/publish).

## First use

The skill checks the machine, offers storage and model choices, then bootstraps
a private Python 3.12 runtime through uv. Dependency installation and model
downloads require internet access. Installing the plugin itself does not download
model weights or index any files. Only index files or folders you select.

| Choice | Intended use |
| --- | --- |
| SQLite + sqlite-vec + FTS5 | Default laptop setup; no database server |
| PostgreSQL + pgvector | An existing server or a dedicated local cluster |
| UltraBERT-Embed-150M | Smaller English embedding model |
| Nemotron-3-Embed-1B-BF16 | Larger multilingual embedding model, run on CPU |

The machine recommendation considers memory, language, and your preference.
It is a memory-fit heuristic, not a universal accuracy ranking or an Intel i5
performance guarantee. Model weights and runtime dependencies use additional
disk space. See [setup and prerequisites](skills/documents/references/setup.md).

PostgreSQL requires pgvector. You can supply a database URL through
`SNYTRO_POSTGRES_URL` or provide PostgreSQL binaries with matching pgvector.
Optional automatic PostgreSQL installation is macOS/Homebrew only and requires
Homebrew to be installed already. SQLite needs neither Homebrew nor PostgreSQL.

## Manual skill installation

Clone this repository, then copy the **whole** `skills/documents` folder into
`~/.agents/skills/documents` for Codex or `~/.claude/skills/documents` for Claude
Code. Choose a plugin installation instead if another skill already uses that
name. Invoke `$documents` in Codex or `/documents` in Claude Code. Do not copy
only `SKILL.md`; its scripts, references, runtime, license, and notice are required.

## CLI usage

From a repository checkout on macOS/Linux:

```bash
bash skills/documents/scripts/bootstrap.sh
bash skills/documents/scripts/snytro doctor
bash skills/documents/scripts/bootstrap.sh --embeddings
bash skills/documents/scripts/snytro setup --storage sqlite --model auto
bash skills/documents/scripts/snytro model-download
bash skills/documents/scripts/snytro --collection contracts index "/path/to/selected/contracts"
bash skills/documents/scripts/snytro --collection contracts status JOB_ID
bash skills/documents/scripts/snytro --collection contracts search "cancellation notice" --stage chunks --semantic
```

On Intel Macs, `--embeddings` installs the smaller ONNX Runtime backend and
`setup --model auto` selects English BGE-small v1.5. On Linux or Apple Silicon,
install the BGE backend before selecting it explicitly:

```bash
bash skills/documents/scripts/bootstrap.sh --bge-small
bash skills/documents/scripts/snytro setup --storage sqlite --model bge-small
bash skills/documents/scripts/snytro model-download
```

BGE-small produces 384-dimensional vectors without PyTorch. `model-download`
downloads its model separately from the runtime dependencies.

For Nemotron, choose `--model nemotron`. Indexing returns a durable job ID; poll
status and resume interrupted work with `resume JOB_ID`. The host agent builds
hierarchical summaries and optional structured fields, then answers with sources.
Initial extractive previews are labeled, and text/keyword retrieval can work while
semantic indexing or agent analysis is incomplete. The CLI's `search` command
returns evidence; the host agent writes the final answer.

The invoking GPT/Codex or Claude agent also acts as the **relevance judge**. Search
results are candidates, not guaranteed matches. The skill instructs the agent to
check requirements, exclusions, and field context; reject contradicted matches;
inspect unresolved previews more deeply; and deduplicate repeated passages before
answering. For example, a male-vocal prompt mentioning female vocals in its exclusion
list does not qualify as a female-vocal recommendation. See the shipped
[relevance workflow](skills/documents/references/relevance.md).

The CLI emits JSON. Collection and data-directory flags go **before** the command.
See [command reference](skills/documents/references/commands.md) and
[agent analysis](skills/documents/references/analysis.md).

## Data and privacy

User indexes, settings, runtime, model cache, and job logs live in the user's
SnytroCLI application-data directory, **outside the plugin**. Set `SNYTRO_DATA_DIR`
to use another location. Plugin upgrades preserve that external data. Storage
migration between SQLite and PostgreSQL is not implemented.

Retrieval stays in the chosen collection and selected document IDs. Changed files
invalidate old analysis and extracted fields. PostgreSQL uses dedicated
`snytro_*` tables; collections are retrieval scopes, not database user
authentication or multi-user access controls.

Extraction, embeddings, and storage are local. Text retrieved into Codex or
Claude Code is processed under that host/provider's data handling. Local storage
does not mean the entire agent workflow is offline. No cloud-drive integration,
email sending, or other cloud actions are included.

## Supported files and current limits

PDF, DOCX, text/Markdown, CSV/TSV, JSON, HTML, and XLSX are supported. Scanned PDFs
need OCR, which is not bundled. Spreadsheet formulas are retained, not recalculated.
Citations use physical PDF pages, document units, or sheet/row ranges.

macOS ARM64 is the verified initial platform. Linux and Windows launchers exist
but have not been validated on those platforms; there is no Windows installer.
The 16 GB Intel i5 target still needs performance testing.
Keyword search remains broad, multilingual tokenization can yield fragment matches,
and reindexing a folder does not automatically prune records for deleted files.
The agent's relevance review improves selection after retrieval; it does not replace
tokenizer or index-maintenance work, or guarantee recall beyond inspected candidates.

## Updates

```bash
codex plugin marketplace upgrade snytro
claude plugin update snytro-search-boost@snytro
```

For Codex, refresh/reinstall the plugin from the Snytro marketplace if the client
still has the previous cached version. Start a new session after updating. Releases
increment the version in all plugin manifests and include a GitHub tag and ZIP.

Desktop users who installed from an extracted ZIP should download the new asset,
extract it as their project marketplace, and reinstall the plugin from that source.
Claude account-marketplace users can refresh/sync the Snytro marketplace and
install its updated version in Plugins. Start a new Local Code session afterward.

The desktop directions are checked against the linked official guides. Installer
commands were tested in isolated configurations; the desktop GUI installation
flows and Claude account sync have not been exercised end-to-end for this release.

## Development

The runtime is self-contained and imports nothing from Snytro File Manager.
Run the tests with the bootstrapped runtime's Python:

```bash
python -m unittest discover -s skills/documents/tests -v
claude plugin validate --strict .
claude plugin validate --strict .claude-plugin/plugin.json
```

PostgreSQL tests require `SNYTRO_TEST_POSTGRES_URL` pointing to a disposable
pgvector database. They never use the desktop app database automatically.

Before packaging, SQLite and an isolated PostgreSQL 17/pgvector cluster passed
32 regression tests on macOS ARM64 with managed Python 3.12. Both embedding
models passed real CPU query/document inference and a two-document semantic
retrieval smoke test. These checks establish functionality, not corpus accuracy.

## License and attribution

The skill and CLI are licensed under [Apache License 2.0](LICENSE). Attribution
to **Snytro — https://snytro.com** is included in [NOTICE](NOTICE). Preserve
applicable notices when redistributing under the license. Apache does not require
a branding label in every application or generated answer.

Downloaded models and third-party dependencies retain their own licenses; model
weights are not included in releases.
