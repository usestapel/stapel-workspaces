# stapel-workspaces

[![CI](https://img.shields.io/github/actions/workflow/status/usestapel/stapel-workspaces/ci.yml?branch=main&logo=github&label=CI)](https://github.com/usestapel/stapel-workspaces/actions/workflows/ci.yml?query=branch%3Amain)
[![coverage](https://img.shields.io/codecov/c/github/usestapel/stapel-workspaces?branch=main&logo=codecov&label=coverage)](https://app.codecov.io/gh/usestapel/stapel-workspaces)
[![pypi](https://img.shields.io/pypi/v/stapel-workspaces?logo=pypi&logoColor=white&label=pypi)](https://pypi.org/project/stapel-workspaces/)
[![downloads](https://static.pepy.tech/badge/stapel-workspaces/month)](https://pepy.tech/project/stapel-workspaces)
[![python](https://img.shields.io/pypi/pyversions/stapel-workspaces?logo=python&logoColor=white)](https://pypi.org/project/stapel-workspaces/)
[![license](https://img.shields.io/github/license/usestapel/stapel-workspaces)](https://github.com/usestapel/stapel-workspaces/blob/main/LICENSE)
[![llms.txt](https://img.shields.io/badge/llms.txt-blue)](https://github.com/usestapel/stapel-workspaces/blob/main/docs/llms.txt)

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
