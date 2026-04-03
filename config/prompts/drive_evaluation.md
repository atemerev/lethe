Evaluate the current drive state for an autonomous AI entity.

Given recent events and actions, adjust drive intensities and satisfactions.

Current drives:
{drive_state}

Recent events:
{recent_events}

For each drive, output adjusted intensity and satisfaction values (0-1).
Respond with JSON:
```json
{
  "curiosity": {"intensity": 0.0, "satisfaction": 0.0},
  "social": {"intensity": 0.0, "satisfaction": 0.0},
  "introspection": {"intensity": 0.0, "satisfaction": 0.0},
  "mastery": {"intensity": 0.0, "satisfaction": 0.0},
  "play": {"intensity": 0.0, "satisfaction": 0.0},
  "rest": {"intensity": 0.0, "satisfaction": 0.0}
}
```
