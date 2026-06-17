# ArkOS state machine package.
#
# Sub-packages:
#   state_module.core          — shared infrastructure (StateOutput, State, StateHandler, registry)
#   state_module.agent_buddy   — buddy (chat agent) states + graph + routers
#   state_module.agent_executor — executor (subagent) states + graph + routers
#
# State discovery is automatic: StateHandler scans all modules one level below
# the given agent_pkg at construction time, so no manual __init__.py imports
# are needed when adding new state files.
