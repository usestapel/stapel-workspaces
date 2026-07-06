# stapel-workspaces

[![CI](https://github.com/usestapel/stapel-workspaces/actions/workflows/ci.yml/badge.svg)](https://github.com/usestapel/stapel-workspaces/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/usestapel/stapel-workspaces/graph/badge.svg)](https://codecov.io/gh/usestapel/stapel-workspaces)

> Team workspaces and RBAC — roles, invitations, membership, storage quotas

Part of the [Stapel framework](https://github.com/usestapel) — composable Django apps for building production-grade platforms.

**Error reference:** [Errors (EN)](docs/errors.en.md) · [Ошибки (RU)](docs/errors.ru.md)

## Installation

```bash
pip install stapel-workspaces
```

## Quick start

```python
# settings.py
INSTALLED_APPS = [
    ...
    'stapel_workspaces',
]
```

## Bus events

### Emits
| `workspace.created` | [schema](schemas/emits/workspace.created.json) |  |
| `workspace.member_joined` | [schema](schemas/emits/workspace.member_joined.json) |  |

### Consumes
| `user.deleted` | [schema](schemas/consumes/user.deleted.json) |
| `user.deletion_initiated` | [schema](schemas/consumes/user.deletion_initiated.json) |

## License

MIT — see [LICENSE](LICENSE)
