# Fictional client pack

This directory demonstrates the shape and registration path required of a separate private client package. It contains no real customer data or proprietary configuration.

The public manifest and process definition are mirrored into `tiramisu_agents.builtin` so they are present in editable installs and wheels. A contract test rejects drift between the public example and those runtime package resources. This directory will become an independently installable editable package when third-party extension loading and lifecycle controls are implemented.
