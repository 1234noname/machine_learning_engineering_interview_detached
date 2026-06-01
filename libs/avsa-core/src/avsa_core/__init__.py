"""AVSA shared foundation package.

Houses the runtime config loader (``avsa_core.config``) and the storage
abstraction (``avsa_core.storage``) used by both the FastAPI gateway
(``avsa_api``) and the offline data-prep library (``avsa_data``). Depends on
the standard library only, so neither consumer pulls heavy deps through it.
"""
