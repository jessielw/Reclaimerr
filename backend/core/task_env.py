from __future__ import annotations

# Environment flags shared between the API process and the isolated task
# children it spawns. They live here, away from ``task_process``, so modules
# that ``task_process`` itself imports (the logger in particular) can tell they
# are running inside a child without importing the task machinery back.
#
# ``desktop/__main__.py`` deliberately keeps its own copy of the child argument:
# it has to branch on it before any backend module is imported.

TASK_CHILD_ENV = "RECLAIMERR_TASK_CHILD"
TASK_ISOLATION_ENV = "RECLAIMERR_TASK_ISOLATION"
TASK_CHILD_ARG = "--task-child"
