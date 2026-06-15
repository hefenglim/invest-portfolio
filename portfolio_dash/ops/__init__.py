"""Low-level operational utilities (backup, integrity, snapshots).

A leaf layer above ``shared``: it imports only stdlib + ``portfolio_dash.shared``
(architecture.md). Higher layers (``scheduler``, etc.) import ``ops``; ``ops`` never
imports them back.
"""
