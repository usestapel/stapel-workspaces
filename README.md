# stapel-workspaces

> Team workspaces and RBAC — roles, invitations, membership, storage quotas

Part of the [Stapel framework](https://github.com/usestapel) — composable Django apps for building production-grade platforms.

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

## Contributing

The source for this package lives inside the [client-project-backend](https://github.com/UCSoftworks) monorepo as a git submodule.

## License

MIT — see [LICENSE](LICENSE)
