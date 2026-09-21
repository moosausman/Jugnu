"""Jugnu backend tests. Run from PythonBackend/:

    PythonEnv/bin/python -m unittest discover -s tests -v

Every test process uses a throwaway JUGNU_DATA_DIR (see _env.py), so tests
never touch the real conversation, memories, permissions or audit log.
"""
