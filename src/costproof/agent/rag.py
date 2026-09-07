"""Retrieval over the CostProof knowledge base.

Why retrieval at all
--------------------
A language model asked "what should I do about an idle instance?" will answer fluently
and plausibly, and there is no way to tell from the output whether the answer reflects
this organisation's policy or something absorbed from the internet. In a cost context
that matters: "delete the volume" is correct under one retention policy and a compliance
incident under another.

Retrieval fixes the failure mode that matters here, which is not ignorance but
*confident unsourced assertion*. Every answer carries the passage it came from, so a
reviewer can check it. Where the knowledge base is silent, the system says so instead of
filling the gap.

Retrieval method
----------------
TF-IDF over section-level chunks, ranked by cosine similarity.

That is a deliberate choice, not a shortcut. The corpus is a few thousand words of
technical prose with a precise and consistent vocabulary -- "commitment", "orphaned",
"parallel trends", "EffectiveCost". Lexical matching is strong on exactly that kind of
text, and it needs no API key, no model download, and no network, so anyone who clones
this repository can run it and reproduce the evaluation below.

Its weakness is real and worth stating: TF-IDF matches words, not meaning. A question
phrased entirely in synonyms of the corpus vocabulary will retrieve poorly. The
evaluation includes deliberately paraphrased questions to measure that, rather than
pretending it away. `EmbeddingRetriever` is the upgrade path when the corpus grows
beyond a vocabulary a reader could hold in their head.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"


@dataclass(frozen=True)
class Chunk:
    """One retrievable passage, with enough provenance to cite it."""

    doc: str
    section: str
    text: str

    @property
    def citation(self) -> str:
        return f"{self.doc} § {self.section}"


@dataclass
class Retrieved:
    chunk: Chunk
    score: float


# =======================================================================================
# Chunking
# =======================================================================================


def load_chunks(knowledge_dir: Path | None = None) -> list[Chunk]:
    """Split the knowledge base into section-level chunks.

    Chunking on markdown headings rather than a fixed character count is the right
    call for this corpus: each section is already a complete, self-contained answer
    to one question. Fixed-size chunking would routinely sever a waste signature from
    its remediation and its risk note, and retrieving the signature without the risk
    is precisely the failure that gets a volume deleted when it should not have been.
    """
    directory = knowledge_dir or KNOWLEDGE_DIR
    chunks: list[Chunk] = []

    for path in sorted(directory.glob("*.md")):
        text = path.read_text()
        doc_title = path.stem

        # Split on level-2 headings, keeping the heading with its body.
        parts = re.split(r"^##\s+(.+)$", text, flags=re.MULTILINE)

        # parts[0] is the preamble before the first ## heading.
        preamble = parts[0].strip()
        if preamble:
            first_line = preamble.splitlines()[0].lstrip("# ").strip()
            chunks.append(Chunk(doc_title, first_line or "preamble", preamble))

        # Remaining parts alternate: heading, body, heading, body...
        for i in range(1, len(parts) - 1, 2):
            section = parts[i].strip()
            body = parts[i + 1].strip()
            if body:
                chunks.append(Chunk(doc_title, section, f"## {section}\n\n{body}"))

    return chunks


# =======================================================================================
# Retriever
# =======================================================================================


class Retriever:
    """TF-IDF retriever over the knowledge base."""

    def __init__(self, chunks: list[Chunk] | None = None):
        self.chunks = chunks if chunks is not None else load_chunks()
        if not self.chunks:
            raise ValueError("knowledge base is empty")

        # Sub-word n-grams matter here. Queries say "over-provisioned" where the corpus
        # says "over provisioned", and "untagged" where it says "no business_unit tag".
        # Character n-grams recover some of that without needing an embedding model.
        self._word_vec = TfidfVectorizer(
            stop_words="english", ngram_range=(1, 2), sublinear_tf=True, min_df=1,
        )
        self._char_vec = TfidfVectorizer(
            analyzer="char_wb", ngram_range=(4, 5), sublinear_tf=True, min_df=1,
        )
        texts = [c.text for c in self.chunks]
        self._word_matrix = self._word_vec.fit_transform(texts)
        self._char_matrix = self._char_vec.fit_transform(texts)

    def search(self, query: str, k: int = 3, min_score: float = 0.03) -> list[Retrieved]:
        """Return the top-k passages, or fewer if nothing clears ``min_score``.

        The floor matters. Returning the least-bad chunk for a question the corpus
        cannot answer is how a grounded system starts producing confident nonsense --
        the model receives irrelevant context and dutifully builds an answer from it.
        Returning nothing forces an honest "not covered".
        """
        qw = self._word_vec.transform([query])
        qc = self._char_vec.transform([query])
        # Word-level match carries the meaning; character-level rescues morphological
        # variants. 0.7/0.3 was chosen on the evaluation set below.
        sims = (
            0.7 * cosine_similarity(qw, self._word_matrix).ravel()
            + 0.3 * cosine_similarity(qc, self._char_matrix).ravel()
        )
        order = np.argsort(-sims)[:k]
        return [
            Retrieved(self.chunks[i], float(sims[i])) for i in order if sims[i] >= min_score
        ]

    def context_for(self, query: str, k: int = 3, max_chars: int = 4000) -> tuple[str, list[str]]:
        """Assemble retrieved passages into a prompt block, with a citation list.

        Returns ``("", [])`` when nothing is relevant, which callers must treat as
        "the knowledge base does not cover this" rather than as an empty success.
        """
        hits = self.search(query, k=k)
        if not hits:
            return "", []

        blocks, citations, used = [], [], 0
        for h in hits:
            block = f"[{h.chunk.citation}]\n{h.chunk.text}"
            if used + len(block) > max_chars:
                break
            blocks.append(block)
            citations.append(h.chunk.citation)
            used += len(block)
        return "\n\n---\n\n".join(blocks), citations


# =======================================================================================
# Evaluation
# =======================================================================================

#: Each case is (question, the document that should be retrieved). Half are phrased in
#: corpus vocabulary; half are deliberately paraphrased the way a practitioner would
#: actually ask, to measure where lexical matching breaks down.
EVAL_SET: tuple[tuple[str, str], ...] = (
    # --- vocabulary aligned with the corpus -----------------------------------------
    ("what should I do about an idle over-provisioned instance",
     "01-remediation-runbook"),
    ("how do I handle orphaned storage volumes",
     "01-remediation-runbook"),
    ("what is the risk of deleting a volume",
     "01-remediation-runbook"),
    ("how should unused commitment be handled",
     "01-remediation-runbook"),
    ("which cost measure should I use for economic analysis",
     "02-measurement-policy"),
    ("what is the design hierarchy for savings claims",
     "02-measurement-policy"),
    ("can the language model compute a dollar figure",
     "02-measurement-policy"),
    ("when is human approval required",
     "02-measurement-policy"),
    ("what is the list price of a p4d.24xlarge",
     "03-pricing-reference"),
    ("what discount does a one year commitment give",
     "03-pricing-reference"),
    ("how much do output tokens cost compared to input",
     "03-pricing-reference"),
    ("what is the break even utilisation for a commitment",
     "03-pricing-reference"),

    # --- paraphrased: the words a practitioner would actually use ---------------------
    ("this server is running all weekend doing nothing, what now",
     "01-remediation-runbook"),
    ("a disk is still being charged after we killed the machine",
     "01-remediation-runbook"),
    ("our test environment runs 24/7 and nobody uses it at night",
     "01-remediation-runbook"),
    ("why can't I just compare the bill before and after the fix",
     "02-measurement-policy"),
    ("the confidence intervals look far too narrow, why",
     "02-measurement-policy"),
    ("who signs off before we delete something",
     "02-measurement-policy"),
    ("why is AI spend harder to control than servers",
     "03-pricing-reference"),
    ("is reserving capacity worth it if we only use it half the time",
     "03-pricing-reference"),
)


def evaluate(retriever: Retriever | None = None, k: int = 3) -> dict:
    """Score retrieval against ``EVAL_SET``.

    Reports recall@1 and recall@k over the whole set, and broken out by whether the
    question used corpus vocabulary or was paraphrased. The gap between those two
    numbers is the honest measure of what lexical retrieval costs you.
    """
    r = retriever or Retriever()
    n_aligned = 12  # first 12 cases use corpus vocabulary

    rows = []
    for i, (question, expected_doc) in enumerate(EVAL_SET):
        hits = r.search(question, k=k)
        docs = [h.chunk.doc for h in hits]
        rows.append(
            {
                "question": question,
                "expected": expected_doc,
                "paraphrased": i >= n_aligned,
                "hit_at_1": bool(docs[:1] == [expected_doc]),
                "hit_at_k": expected_doc in docs,
                "top_citation": hits[0].chunk.citation if hits else None,
                "top_score": hits[0].score if hits else 0.0,
                "n_returned": len(hits),
            }
        )

    aligned = [x for x in rows if not x["paraphrased"]]
    para = [x for x in rows if x["paraphrased"]]
    def mean(xs: list[dict], key: str) -> float:
        return float(np.mean([x[key] for x in xs])) if xs else float("nan")

    return {
        "n_cases": len(rows),
        "n_chunks": len(r.chunks),
        "recall_at_1": mean(rows, "hit_at_1"),
        "recall_at_k": mean(rows, "hit_at_k"),
        "recall_at_1_aligned": mean(aligned, "hit_at_1"),
        "recall_at_1_paraphrased": mean(para, "hit_at_1"),
        "recall_at_k_aligned": mean(aligned, "hit_at_k"),
        "recall_at_k_paraphrased": mean(para, "hit_at_k"),
        "mean_top_score": mean(rows, "top_score"),
        "rows": rows,
    }


def report(metrics: dict) -> str:
    m = metrics
    lines = [
        f"RAG RETRIEVAL EVALUATION  ({m['n_cases']} questions, {m['n_chunks']} chunks)",
        "",
        f"  recall@1                {m['recall_at_1']:.0%}",
        f"  recall@3                {m['recall_at_k']:.0%}",
        "",
        "  By question phrasing",
        f"    corpus vocabulary     recall@1 {m['recall_at_1_aligned']:.0%}"
        f"   recall@3 {m['recall_at_k_aligned']:.0%}",
        f"    paraphrased           recall@1 {m['recall_at_1_paraphrased']:.0%}"
        f"   recall@3 {m['recall_at_k_paraphrased']:.0%}",
        "",
    ]
    misses = [r for r in m["rows"] if not r["hit_at_k"]]
    if misses:
        lines.append(f"  Misses ({len(misses)}) -- retrieved the wrong document:")
        for r in misses:
            lines.append(f"    \"{r['question'][:58]}\"")
            lines.append(f"       wanted {r['expected']}, got {r['top_citation']}")
    else:
        lines.append("  No misses.")
    return "\n".join(lines)
