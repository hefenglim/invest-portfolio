"""Child-process entry points for the ``child_runner`` / ``proc_lock`` tests.

Each module is run as ``python -m tests.shared.child_fixtures.<name> <args>`` from the
repository root (``child_runner.package_root()``), so it must stay cheap to import: no app, no
database. They talk back through files in the test's ``tmp_path``, never through stdout (the
runner discards it).
"""
