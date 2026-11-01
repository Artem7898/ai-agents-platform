# AI Agents Platform documentation

Start with the repository [README](https://github.com/Artem7898/ai-agents-platform#readme) or [Russian setup guide](https://github.com/Artem7898/ai-agents-platform/blob/main/README_RU.md).

- [Operations and recovery](operations.md): release, worker, backup and recovery procedures.
- Generated development API schemas: `/api/v2/docs`.
- Domain contracts: `src/agents/schemas.py`, `src/workflows/schemas.py`, `src/runs/schemas.py`.

The application provides persisted workflow execution and inspection. External effects require idempotent tools. There is no arbitrary rollback API and no verified deployment-specific latency or availability guarantee.
