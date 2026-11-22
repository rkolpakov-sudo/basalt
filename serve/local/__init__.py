"""Local additions to Basalt: the version block, the local state file, the updater and the models tab.

Upstream knows nothing about this package.  Everything here is additive: `serve/handler.py` gains one
import and one branch per HTTP verb, and no upstream module is edited, so an upstream release merges
without a conflict here.
"""
