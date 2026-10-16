"""Adapters to the systems Mneme serves. Each imports its system only when used, so the core needs none of them.

- `ontodb`: OntoDB as a storage engine, through its shell.
- `aegis`: ontology versions as memories; an Aegis rollback invalidates what was built on undone versions.
- `moira`: MOIRA's three-tier query cache as a cache over the store, and a front desk that asks through it.
- `kroma`: KROMA's online refinement as a matching agent on the blackboard.
"""
