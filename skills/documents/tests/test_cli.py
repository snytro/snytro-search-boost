# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "runtime" / "src"
sys.path.insert(0, str(SOURCE))

from snytro_cli.cli import execute, parser, status
from snytro_cli.embeddings import model_key, validate_vector
from snytro_cli.engine import Engine, collection_lock
from snytro_cli.extract import discover, extract_units, text_chunks
from snytro_cli.handover import export_collection, restore_collection
from snytro_cli.profiles import install_profile
from snytro_cli.settings import atomic_json, load_settings


class FakeEmbedder:
    """Deterministic dense vectors test storage/filtering, not model quality."""
    dimensions = 768

    def chunks(self, content):
        return text_chunks(content)

    def encode(self, texts, query=False):
        output = []
        for text in texts:
            vector = [0.0] * self.dimensions
            vector[0 if "invoice" in text.lower() else 1] = 1.0
            output.append(vector)
        return output


class RetrievalContract:
    postgres = False

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.collection = "test_" + uuid.uuid4().hex
        self.settings = {"version": 1, "storage": "postgres" if self.postgres else "sqlite", "model": "ultrabert", "model_revision": "test-revision", "chunk_tokens": 512, "batch_size": 1, "threads": 2}
        self.environment = None
        if self.postgres:
            url = os.environ.get("SNYTRO_TEST_POSTGRES_URL")
            if not url:
                self.temporary.cleanup()
                self.skipTest("Set SNYTRO_TEST_POSTGRES_URL to a disposable pgvector database.")
            self.environment = patch.dict(os.environ, {"SNYTRO_POSTGRES_URL": url})
            self.environment.start()
        self.engine = Engine(self.root, self.settings, self.collection)
        self.engine._embedder = FakeEmbedder()
        self.engine.store.initialize()
        self.invoice = self.root / "invoice.txt"
        self.invoice.write_text("Invoice INV-1. Total AUD 1250. Payment is due in thirty days.")
        self.contract = self.root / "contract.txt"
        self.contract.write_text("Contract for office rental. Notice period is ninety days.")

    def tearDown(self):
        if self.postgres and self.environment:
            with self.engine.store.connection() as connection:
                rows = self.engine.store.execute(connection, "SELECT id FROM snytro_documents WHERE collection IN (?,?)", (self.collection, self.collection + "_other")).fetchall()
                for row in rows:
                    connection.execute("DELETE FROM snytro_documents WHERE id=%s", (row["id"],))
                connection.execute("DELETE FROM snytro_jobs WHERE collection=%s", (self.collection,))
            self.environment.stop()
        self.temporary.cleanup()

    def index(self):
        return self.engine.index_file(self.invoice), self.engine.index_file(self.contract)

    def test_scope_is_enforced_before_vector_top_k(self):
        invoice, contract = self.index()
        other = Engine(self.root, self.settings, self.collection + "_other")
        other._embedder = FakeEmbedder()
        foreign = other.index_file(self.invoice)
        vector = FakeEmbedder().encode(["invoice"])[0]
        results = self.engine.store.semantic(self.collection, vector, model_key(self.settings), [contract["id"]], 1)
        self.assertEqual([row["document_id"] for row in results], [contract["id"]])
        for ids in ([], [foreign["id"]]):
            self.assertEqual(self.engine.store.semantic(self.collection, vector, model_key(self.settings), ids, 1), [])
            self.assertEqual(self.engine.search("invoice", "chunks", ids, 1, False), [])
        self.assertEqual(self.engine.store.documents(self.collection, []), [])

    def test_analysis_preserves_original_and_keyword_chunk_catalog(self):
        invoice, _ = self.index()
        self.engine.save_analysis(invoice["id"], {"checksum": invoice["checksum"], "tagline": "Accounts receivable", "summary": "Payment due in thirty days.", "keywords": ["invoice"], "units": [{"number": 1, "summary": "Invoice total is AUD 1250.", "tagline": "Invoice payment"}]})
        record = self.engine.store.document(self.collection, invoice["id"])
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["units"][0]["content"], self.invoice.read_text())
        self.assertTrue(self.engine.search("1250", "chunks", [invoice["id"]], 5, False))
        self.assertEqual(self.engine.search("receivable", "taglines", [invoice["id"]], 5, False)[0]["summary_kind"], "agent_analysis")

    def test_changed_content_clears_old_extraction_and_vectors(self):
        invoice, _ = self.index()
        self.engine.save_extraction(invoice["id"], {"checksum": invoice["checksum"], "schema": "invoice", "source_units": [1], "fields": {"total": 1250}})
        self.invoice.write_text("Contract replacement. Monthly rent is AUD 2400.")
        new = self.engine.index_file(self.invoice)
        record = self.engine.store.document(self.collection, invoice["id"])
        self.assertNotEqual(new["checksum"], invoice["checksum"])
        self.assertEqual(record["extractions"], {})
        self.assertEqual(record["summary_kind"], "extractive_preview")
        self.assertFalse(self.engine.search("1250", "chunks", [invoice["id"]], 5, False))
        vector = FakeEmbedder().encode(["invoice"])[0]
        results = self.engine.store.semantic(self.collection, vector, model_key(self.settings), [invoice["id"]], 1)
        self.assertGreater(results[0]["distance"], 0.9)

    def test_checksum_and_provenance_reject_stale_analysis(self):
        invoice, _ = self.index()
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.engine.save_analysis(invoice["id"], {"checksum": "stale", "tagline": "Wrong", "summary": "Wrong"})
        with self.assertRaisesRegex(ValueError, "source_units"):
            self.engine.save_extraction(invoice["id"], {"checksum": invoice["checksum"], "schema": "invoice", "fields": {}, "source_units": [99]})
        with self.assertRaisesRegex(ValueError, "collection"):
            self.engine.store.document(self.collection + "_other", invoice["id"])

    def test_embedding_failure_retains_keyword_evidence(self):
        self.engine._embedder = None
        with patch("snytro_cli.engine.Embedder", side_effect=ValueError("model unavailable")):
            with self.assertRaisesRegex(ValueError, "Text is indexed"):
                self.engine.index_file(self.invoice)
        records = self.engine.store.documents(self.collection)
        self.assertEqual(records[0]["embedding_status"], "failed")
        self.assertTrue(self.engine.search("invoice", "chunks", None, 5, False))

    def test_failed_update_does_not_serve_stale_content(self):
        invoice, _ = self.index()
        self.invoice.write_text("")
        job_id = str(uuid.uuid4())
        self.engine.store.save_job({"id": job_id, "collection": self.collection, "status": "queued", "paths": [str(self.invoice)], "recursive": True, "lexical_only": True})
        job = self.engine.run_job(job_id)
        self.assertEqual(job["status"], "completed_with_errors")
        self.assertFalse(self.engine.search("1250", "chunks", [invoice["id"]], 5, False))

    def test_restart_persistence_and_unchanged_checksum(self):
        invoice, _ = self.index()
        reopened = Engine(self.root, self.settings, self.collection)
        reopened._embedder = FakeEmbedder()
        self.assertEqual(reopened.index_file(self.invoice)["index_action"], "unchanged")
        self.assertTrue(reopened.search("invoice", "chunks", [invoice["id"]], 5, True))

    def test_function_words_do_not_override_semantic_rank(self):
        invoice, contract = self.index()
        rows = self.engine.search("How much advance warning is required to cancel?", "chunks", None, 2, True)
        self.assertEqual(rows[0]["document_id"], contract["id"])

    def test_model_switch_does_not_mix_embedding_spaces(self):
        invoice, _ = self.index()
        new_settings = {**self.settings, "model_revision": "different-revision"}
        vector = FakeEmbedder().encode(["invoice"])[0]
        self.assertEqual(self.engine.store.semantic(self.collection, vector, model_key(new_settings), [invoice["id"]], 5), [])

    def test_nemotron_dimensions_are_supported_and_scoped(self):
        class NemotronFake(FakeEmbedder):
            dimensions = 2048
        self.engine.settings = {**self.settings, "model": "nemotron"}
        self.engine._embedder = NemotronFake()
        record = self.engine.index_file(self.invoice)
        vector = NemotronFake().encode(["invoice"])[0]
        rows = self.engine.store.semantic(self.collection, vector, model_key(self.engine.settings), [record["id"]], 5)
        self.assertEqual(rows[0]["document_id"], record["id"])

    def test_vector_replacement_rolls_back_atomically(self):
        invoice, _ = self.index()
        record = self.engine.store.document(self.collection, invoice["id"])
        chunks = self.engine.make_chunks(record, semantic=True)
        chunks[0]["embedding"] = [float("nan")] * 768
        with self.assertRaises(ValueError):
            self.engine.store.save_document(record, chunks)
        vector = FakeEmbedder().encode(["invoice"])[0]
        self.assertTrue(self.engine.store.semantic(self.collection, vector, model_key(self.settings), [invoice["id"]], 5))
        self.assertTrue(self.engine.search("1250", "chunks", [invoice["id"]], 5, False))

    def test_remove_clears_all_search_surfaces(self):
        invoice, _ = self.index()
        self.engine.store.remove(self.collection, [invoice["id"]])
        self.assertTrue(self.invoice.exists())
        self.assertFalse(self.engine.search("invoice", "taglines", [invoice["id"]], 5, False))
        self.assertFalse(self.engine.search("invoice", "chunks", [invoice["id"]], 5, True))

    def test_hierarchy_ranks_and_filters_sources_before_top_k(self):
        install_profile(self.root, self.collection, {
            "sources": {
                self.invoice.name: {"rank": 3, "visibility": "explicit_only"},
                self.contract.name: {"rank": 1, "visibility": "default"},
            },
            "routes": {"rental": [self.contract.name]},
        }, self.root)
        self.engine = Engine(self.root, self.settings, self.collection)
        self.engine._embedder = FakeEmbedder()
        for path in (self.invoice, self.contract):
            record = self.engine.index_file(path)
            self.engine.save_analysis(record["id"], {
                "checksum": record["checksum"], "tagline": "invoice context",
                "summary": "Terms and conditions", "units": [
                    {"number": 1, "tagline": "invoice conditions", "summary": "Commercial details"}
                ],
            })
            self.engine.index_file(path)
        for stage in ("taglines", "summaries", "pages", "chunks"):
            rows = self.engine.search("invoice", stage, None, 1, True, dense_only=True)
            self.assertEqual([r["source_file_name"] for r in rows], [self.contract.name])
            self.assertLess(rows[0]["distance"], 0.01)
            expanded = self.engine.search("invoice", stage, None, 2, True, dense_only=True, include_explicit=True)
            self.assertEqual([r["source_file_name"] for r in expanded], [self.contract.name, self.invoice.name])
        hybrid = self.engine.search("commercial", "pages", None, 5, True)
        self.assertEqual(len(hybrid), 1)
        self.assertEqual(hybrid[0]["matched_by"], ["keyword", "semantic"])
        explicit = self.engine.search("INV-1", "chunks", None, 2, False, exact=True, include_explicit=True)
        self.assertEqual([r["source_file_name"] for r in explicit], [self.invoice.name])
        self.assertEqual(self.engine.search("INV-1", "chunks", None, 2, False, exact=True), [])
        self.assertEqual(self.engine.search("INV-1", "chunks", None, 2, False, exact=True, route="rental", include_explicit=True), [])
        self.assertFalse(self.engine.search("invoice", "pages", [], 1, True))

    def test_handover_restores_vectors_policy_and_originals_without_reindexing(self):
        install_profile(self.root, self.collection, {"sources": {
            self.invoice.name: {"rank": 1, "visibility": "default"},
            self.contract.name: {"rank": 6, "visibility": "explicit_only"},
        }}, self.root)
        self.engine = Engine(self.root, self.settings, self.collection)
        self.engine._embedder = FakeEmbedder()
        self.index()
        filename = self.root / "transfer.zip"
        export_collection(self.engine, filename, True)
        target = Engine(self.root / "recipient", {**self.settings, "storage": "sqlite"}, "copy")
        target.store.initialize()
        result = restore_collection(target, filename, None)
        self.assertEqual(result["matching_embedding_documents"], 2)
        target = Engine(target.root, target.settings, "copy")
        target._embedder = FakeEmbedder()
        self.assertEqual((Path(result["source_root"]) / self.invoice.name).read_bytes(), self.invoice.read_bytes())
        self.assertEqual(target.search("invoice", "chunks", None, 5, True, dense_only=True)[0]["source_file_name"], self.invoice.name)
        self.assertFalse(target.search("ninety days", "chunks", None, 5, False, exact=True))
        with self.assertRaisesRegex(ValueError, "empty collection"):
            restore_collection(target, filename, None)

    def test_interrupted_job_is_explicit_and_resumable(self):
        job_id = str(uuid.uuid4())
        self.engine.store.save_job({"id": job_id, "collection": self.collection, "status": "running", "pid": 2147483647, "process_created": 0, "paths": [str(self.invoice)], "recursive": True, "lexical_only": True})
        self.assertEqual(status(self.engine.store, self.collection, job_id)["status"], "interrupted")
        resumed = self.engine.run_job(job_id)
        self.assertEqual(resumed["status"], "completed")
        self.assertNotIn("error", resumed)


class SQLiteContract(RetrievalContract, unittest.TestCase):
    pass


class PostgresContract(RetrievalContract, unittest.TestCase):
    postgres = True


class ExtractionAndCLI(unittest.TestCase):
    def test_api_full_job_uses_only_reviewed_sources_and_resumes_summary_batches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = root / "sources"
            sources.mkdir()
            selected = sources / "policy.txt"
            selected.write_text("\n".join(["Source requires MFA for privileged operators."] * 175))
            (sources / "answer-key.txt").write_text("UNREVIEWED_SECRET")
            command = lambda *args: execute(parser().parse_args(["--data-dir", str(root / "state"), *args]))
            command("setup", "--embedding-provider", "compatible", "--embedding-base-url", "https://vectors.example.test/v1",
                    "--embedding-model", "example-vector", "--embedding-dimensions", "3", "--embedding-key-env", "EXAMPLE_EMBED_KEY",
                    "--summary-provider", "openai")
            profile = root / "policy.json"
            profile.write_text(json.dumps({"sources": {"policy.txt": {"rank": 1, "visibility": "default"}}}))
            command("profile", "--input", str(profile), "--source-root", str(sources))
            calls, fail = [], True

            def post(client, route, body):
                nonlocal fail
                self.assertNotIn("UNREVIEWED_SECRET", json.dumps(body))
                if route == "/embeddings":
                    self.assertEqual(client.config["key_env"], "EXAMPLE_EMBED_KEY")
                    self.assertNotIn("dimensions", body)
                    items = [{"index": i, "embedding": [1, 0, 0]} for i in range(len(body["input"]))]
                    return {"data": items[::-1]}
                self.assertEqual(client.config["key_env"], "OPENAI_API_KEY")
                evidence = json.loads(body["messages"][1]["content"])
                if "units" in evidence:
                    numbers = [item["number"] for item in evidence["units"]]
                    calls.append(numbers)
                    if 2 in numbers and fail:
                        fail = False
                        raise ValueError("Simulated API failure")
                    value = {"units": [{"number": n, "summary": "Privileged operators require MFA.", "tagline": "MFA"} for n in numbers]}
                else:
                    value = {"summary": "Privileged operators require MFA.", "tagline": "Authentication policy"}
                return {"choices": [{"message": {"content": json.dumps(value)}}]}

            with patch("snytro_cli.providers.Client.post", autospec=True, side_effect=post):
                first = command("index", str(sources), "--rich", "--full", "--wait")
                self.assertEqual(first["status"], "completed_with_errors")
                self.assertEqual(first["result"]["excluded"], [str((sources / "answer-key.txt").resolve())])
                records = command("list")["documents"]
                self.assertEqual(records[0]["analysis_status"], "partial")
                command("setup", "--summary-provider", "host")
                with self.assertRaisesRegex(ValueError, "API provider settings changed"):
                    command("_worker", first["id"])
                command("setup", "--summary-provider", "openai")
                result = command("_worker", first["id"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(sum(1 in items for items in calls), 1)
                self.assertEqual(command("verify", "--check-originals")["coverage"]["hierarchy_embeddings_ready"], 1)
                answer = command("search", "login checks", "--stage", "pages", "--dense-only")
                self.assertTrue(answer["results"][0]["locator"]["line_start"] >= 1)
                self.assertEqual(command("fetch", answer["results"][0]["document_id"], "--unit", str(answer["results"][0]["unit"]))["locator"]["kind"], "lines")

    def test_directory_selection_does_not_follow_external_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "selected"
            selected.mkdir()
            outside = root / "outside.txt"
            outside.write_text("Unselected private content")
            inside = selected / "inside.txt"
            inside.write_text("Selected content")
            try:
                (selected / "link.txt").symlink_to(outside)
            except OSError:
                self.skipTest("Symlinks unavailable")
            files, _ = discover([str(selected)])
            self.assertEqual(files, [inside.resolve()])

    def test_docx_body_order_and_xlsx_locators(self):
        from docx import Document
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = Document()
            document.add_paragraph("Before")
            document.add_table(rows=1, cols=1).cell(0, 0).text = "Middle"
            document.add_paragraph("After")
            document.save(root / "file.docx")
            unit = extract_units(root / "file.docx")[0]
            self.assertEqual(unit["content"], "Before\nMiddle\nAfter")
            self.assertNotIn("page", unit["locator"])
            workbook = Workbook()
            workbook.active.title = "Revenue"
            workbook.active.append(["Total", "=SUM(B2:B9)"])
            workbook.save(root / "file.xlsx")
            unit = extract_units(root / "file.xlsx")[0]
            self.assertEqual(unit["locator"]["sheet"], "Revenue")
            self.assertIn("=SUM", unit["content"])

    def test_pdf_preserves_page_number_and_empty_pdf_reports_ocr(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=200)
            writer.write(path)
            with self.assertRaisesRegex(ValueError, "OCR"):
                extract_units(path)

    def test_active_collection_lock_is_not_stolen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with collection_lock(root, "test"):
                with self.assertRaisesRegex(ValueError, "busy"):
                    with collection_lock(root, "test"):
                        pass

    def test_no_config_and_argument_errors_are_json(self):
        with tempfile.TemporaryDirectory() as directory:
            for arguments in (["list"], ["fetch", "missing", "--unit", "0"]):
                result = subprocess.run([sys.executable, "-m", "snytro_cli.cli", "--data-dir", directory, *arguments], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(SOURCE)})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(json.loads(result.stdout)["ok"])

    def test_detached_ingestion_and_fetch_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = {"version": 1, "storage": "sqlite", "model": "none", "chunk_tokens": 512, "batch_size": 1, "threads": 2}
            from snytro_cli.storage import Store
            Store(root, settings).initialize()
            atomic_json(root / "settings.json", settings)
            selected = root / "source.txt"
            selected.write_text("Invoice " * 3000)
            def command(*arguments):
                result = subprocess.run([sys.executable, "-m", "snytro_cli.cli", "--data-dir", str(root), *arguments], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(SOURCE)})
                return json.loads(result.stdout)
            job = command("index", str(selected))
            self.assertTrue(job["ok"])
            deadline = time.time() + 15
            while time.time() < deadline:
                current = command("status", job["job_id"])
                if current["status"] in {"completed", "failed", "completed_with_errors"}:
                    break
                time.sleep(0.1)
            self.assertEqual(current["status"], "completed")
            record = command("list")["documents"][0]
            page = command("fetch", record["id"], "--unit", "1", "--max-chars", "100")
            self.assertEqual(page["next_offset"], 100)
            self.assertEqual(page["content"], selected.read_text()[:100])
            self.assertEqual(command("list", "--document-ids")["documents"], [])


if __name__ == "__main__":
    unittest.main()
