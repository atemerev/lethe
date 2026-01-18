#!/usr/bin/env python3
"""Delete the Lethe agent from Letta Cloud."""

from letta_client import Letta
from lethe.config import get_settings

settings = get_settings()
client = Letta(base_url=settings.letta_base_url, api_key=settings.letta_api_key)

for agent in list(client.agents.list(name=settings.lethe_agent_name)):
    print(f"Deleting agent: {agent.id}")
    client.agents.delete(agent.id)

print("Done")
