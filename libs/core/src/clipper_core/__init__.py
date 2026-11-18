"""Shared building blocks for every clipper service.

Services may only talk to each other through Postgres and object storage. Everything that
defines that contract (table models, job kinds, payloads, storage keys, priorities) lives here.
"""
