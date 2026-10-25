"""Example connector. Copy to ``dice.py`` (no leading underscore) to enable it."""

from nexus.connectors import Connector, tool


class Dice(Connector):
    name = "dice"
    description = "Roll dice"

    @tool()
    async def roll(self, sides: int = 6) -> int:
        """Roll one die with the given number of sides."""
        import random

        return random.randint(1, sides)
