"""AVSA offline data-prep library.

The Fashion200k data pipeline: subset acquisition (``acquisition``), self-
describing manifests + metadata (``fashion200k_metadata``), the embedding
artifact reader/writer (``embedding_pipeline``), catalog seeding helpers
(``catalog_fashion200k``), and linear attribute-head training
(``attribute_heads``). All offline — invoked by ``scripts/`` CLIs, evals, and
integration tests, never by the request-serving gateway. Depends on
``avsa_core`` for the storage abstraction; carries numpy + httpx so the gateway
need not.
"""
