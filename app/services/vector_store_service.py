"""Hybrid document retrieval for the analyst chat: BM25 (exact words) + ChromaDB (meaning), merged by reciprocal rank fusion.

* Source of truth: MySQL `document_chunks` (see repositories/document_chunks.py). ChromaDB is a derived, per-workflow index kept under
  <UPLOAD_DIR>/_chroma and rebuilt whenever the model's document set changes, so any server can serve the same answers.
* Isolation: a workflow only ever searches its own published-version document files (`files` is passed in by the caller after the
  model access check), and Chroma collections are named per workflow.
* Graceful degradation: no documents -> []; ChromaDB missing / embedding model unavailable -> BM25 only; any error -> [] (the chat then
  answers from the data alone). Retrieval never raises into the chat.
"""
import hashlib
import logging
import math
import re
import threading
from collections import Counter, OrderedDict
from pathlib import Path

from ..repositories.document_chunks import DocumentChunkRepository
from . import document_processor as dp

log = logging.getLogger(__name__)

TOP_K = 4
CANDIDATES = 10
MAX_DISTANCE = 0.75   # cosine distance; ~cos >= 0.25 counts as related. Unrelated passages are dropped rather than forced into an answer
RRF_K = 60
STOP = {"the", "a", "an", "of", "in", "on", "for", "to", "is", "are", "was", "were", "what", "which", "who", "how", "why", "did", "does",
        "me", "my", "our", "and", "or", "with", "by", "this", "that", "it", "do", "show", "tell", "give", "about", "has", "have", "be"}

_INDEX = OrderedDict()   # fingerprint -> _WorkflowIndex
_LOCK = threading.Lock()
_INDEX_MAX = 4


def tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+", str(text).lower()) if t not in STOP and len(t) > 1]


class Bm25:
    def __init__(self, docs, k1=1.4, b=0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(tokens(d)) for d in docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 0.0
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def ranked(self, query, n):
        q = tokens(query)
        scored = []
        for i, c in enumerate(self.tf):
            norm = self.k1 * (1 - self.b + self.b * self.len[i] / (self.avg or 1.0))
            s = sum(self.idf.get(t, 0.0) * c[t] * (self.k1 + 1) / (c[t] + norm) for t in q if c.get(t))
            if s > 0:
                scored.append((s, i))
        return [i for _, i in sorted(scored, reverse=True)[:n]]


class _WorkflowIndex:
    def __init__(self, chunks, chroma_path, workflow_id, fp):
        self.chunks = chunks
        self.bm25 = Bm25([c["text"] for c in chunks])
        self.col = self._build_chroma(chroma_path, workflow_id, fp)

    def _build_chroma(self, chroma_path, workflow_id, fp):
        try:
            import chromadb
            client = chromadb.PersistentClient(path=str(chroma_path))
            name = f"wf_{int(workflow_id)}"
            try:
                col = client.get_collection(name)
                if (col.metadata or {}).get("fp") == fp:
                    return col
                client.delete_collection(name)
            except Exception:
                pass
            col = client.create_collection(name, metadata={"hnsw:space": "cosine", "fp": fp})
            for i in range(0, len(self.chunks), 64):
                batch = self.chunks[i:i + 64]
                col.add(ids=[str(i + j) for j in range(len(batch))], documents=[c["text"] for c in batch],
                        metadatas=[{"workflow_id": int(workflow_id), "file_id": int(c["file_id"]), "page": int(c["page"] or 0)} for c in batch])
            return col
        except Exception as e:
            log.warning("ChromaDB unavailable (%s: %s); document retrieval will use BM25 only", type(e).__name__, e)
            return None

    def search(self, question, k):
        ranks = {}
        for r, i in enumerate(self.bm25.ranked(question, CANDIDATES)):
            ranks[i] = ranks.get(i, 0.0) + 1.0 / (RRF_K + r)
        if self.col is not None:
            try:
                res = self.col.query(query_texts=[question], n_results=min(CANDIDATES, len(self.chunks)))
                for r, (cid, dist) in enumerate(zip(res["ids"][0], res["distances"][0])):
                    if dist <= MAX_DISTANCE:
                        ranks[int(cid)] = ranks.get(int(cid), 0.0) + 1.0 / (RRF_K + r)
            except Exception as e:
                log.warning("ChromaDB query failed (%s); using BM25 only", e)
        return [self.chunks[i] for i, _ in sorted(ranks.items(), key=lambda kv: -kv[1])[:k]]


class DocumentSearch:
    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg
        self.repo = DocumentChunkRepository(db)

    # ------------------------------------------------------------------ ingestion
    def index_file(self, file_id, workflow_id, ext, path):
        """Extract + chunk one PDF/DOCX/TXT file into MySQL. Safe to call repeatedly. Returns the number of passages."""
        try:
            chunks = dp.chunk(dp.extract(Path(path), ext))
        except Exception as e:
            log.warning("could not extract text from file %s (%s)", file_id, type(e).__name__)
            chunks = []
        self.repo.replace_for_file(file_id, workflow_id, chunks)
        return len(chunks)

    def _ensure_indexed(self, workflow_id, docs, path_of):
        done = self.repo.indexed_file_ids(workflow_id)
        for f in docs:
            if int(f["id"]) in done:
                continue
            p = path_of(f)
            if p.exists():
                self.index_file(f["id"], workflow_id, f["ext"], p)

    # ------------------------------------------------------------------ retrieval
    def search(self, workflow_id, files, question, path_of, k=TOP_K):
        """Evidence items [{"source": "document", "text", "citation"}] for the question, or [] when there are no documents."""
        docs = [f for f in files if f["ext"] in dp.DOC_EXT]
        if not docs or not str(question or "").strip():
            return []
        try:
            self._ensure_indexed(workflow_id, docs, path_of)
            rows = self.repo.for_files(workflow_id, [int(f["id"]) for f in docs])
            if not rows:
                return []
            names = {int(f["id"]): f["original_name"] for f in docs}
            chunks = [{**r, "file_id": int(r["file_id"]), "name": names.get(int(r["file_id"]), "document")} for r in rows]
            fp = hashlib.sha256("|".join(f"{c['file_id']}:{c['chunk_idx']}:{len(c['text'])}" for c in chunks).encode()).hexdigest()[:16]
            idx = self._index(fp, chunks, workflow_id)
            return [{"source": "document", "text": c["text"][:700],
                     "citation": f"{c['name']}" + (f" p.{c['page']}" if c.get("page") else "")} for c in idx.search(question, k)]
        except Exception:
            log.exception("document retrieval failed for workflow %s; answering from the data only", workflow_id)
            return []

    def _index(self, fp, chunks, workflow_id):
        key = f"{workflow_id}:{fp}"
        with _LOCK:
            idx = _INDEX.get(key)
            if idx is None:
                idx = _WorkflowIndex(chunks, Path(self.cfg.UPLOAD_DIR) / "_chroma", workflow_id, fp)
                _INDEX[key] = idx
                while len(_INDEX) > _INDEX_MAX:
                    _INDEX.popitem(last=False)
            else:
                _INDEX.move_to_end(key)
            return idx
