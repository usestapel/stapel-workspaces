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
