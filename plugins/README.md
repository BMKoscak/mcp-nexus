# Custom connectors

Any `.py` file in this folder that defines a `Connector` subclass is loaded on
startup (and again when you click **Reload plugins** in the Connections tab).
Files whose names start with `_` are ignored, so `_example_dice.py` is just a
template: copy it to `dice.py` to try it.

```python
from nexus.connectors import Connector, tool

class Dice(Connector):
    name = "dice"                 # tools become dice_<method>
    description = "Roll dice"

    @tool()
    async def roll(self, sides: int = 6) -> int:
        """Roll one die with the given number of sides."""
        import random
        return random.randint(1, sides)
```

- Type hints become the tool's JSON Schema; the docstring becomes its description.
  Use `Annotated[str, Field(description="…")]` for per-argument descriptions.
- Raise `ToolError("message")` for errors the model should see.
- `@tool(requires_approval=True, summary="Do X with {arg}")` routes calls through
  the approval gate; `read_only=True` / `destructive=True` / `open_world=True` set MCP hints.
- `@resource("myplugin://thing")` on a zero-argument method exposes an MCP resource.
- Add a nested `Config` Pydantic model for settings editable in the UI
  (`SecretStr` fields are stored encrypted); read them via `self.config`.
- `self.ctx` gives you `settings`, `db`, `crypto`, `bus`, `approvals` and a shared `http` client.

Plugins run in-process with full privileges: only install code you trust.
